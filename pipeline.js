"use strict";
// pipeline.js — detecta vagas novas no IMAP, classifica pelos critérios,
// grava no SQLite, enfileira as elegíveis para o sidecar (host) ler a página
// completa, e imprime as elegíveis + resultado do sidecar (para o n8n notificar).
// Sem dependência de rede no container além do IMAP + LocalAI; a ponte com o
// sidecar é por arquivos (HTTP container→host é bloqueado por firewall).
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const db = require("./db.js");
const { classificar } = require("./classifica.js");
const { indexResultados } = require("./resultados.js");
const { urlsDaVaga } = require("./url-vaga.js");
const { automationLigada, emailLigado } = require("./estado.js");

const FETCH = "/workflows/host/imap/fetch-inbox.js";
const DAYS = process.argv.includes("--days=1") ? "1" : "3";

// Ponte por arquivos (volume /workflows compartilhado com o host):
// - FILA: pipeline (container) enfileira elegíveis; sidecar (host) consome.
// - RES: sidecar grava o resultado (página lida, portal, botão de candidatura).
const FILA = path.join(__dirname, "fila-vagas.jsonl");
const RES = path.join(__dirname, "resultado-vagas.jsonl");


// Enfileira só os elegíveis que ainda não têm resultado do sidecar. Um e-mail
// pode trazer VÁRIAS vagas (o digest InfoJobs manda uma linha "Anacam contrata
// Auxiliar De Ti +1 novas vagas de Analista de Ti"), então vira uma entrada por
// URL — senão só a primeira seria aplicada.
function enfileiraElegiveis(elegiveis) {
  const vistos = indexResultados(RES);
  const linhas = [];
  for (const e of elegiveis) {
    const urls = urlsDaVaga(e.corpo || e.snippet || "", e.url, e.links);
    // Sem página de vaga o sidecar não tem o que abrir: cai no login do
    // Google e a vaga se perde. Melhor avisar do que enfileirar.
    if (!urls.length) {
      console.log(`  … sem URL de vaga, não enfileirado: ${(e.subject || "").slice(0, 50)}`);
      continue;
    }
    for (const url of urls) {
      if (vistos.visto(e.messageId, url)) continue;
      linhas.push(JSON.stringify({
        messageId: e.messageId,
        idVaga: e.idVaga,
        subject: e.subject,
        from: e.from,
        url,
        local: e.local || "",
        regime: e.regime || "",
      }));
    }
  }
  if (!linhas.length) return;
  fs.appendFileSync(FILA, linhas.join("\n") + "\n", "utf8");
}

// Espera até ~25s o resultado do sidecar para cada elegível (sidecar processa a
// fila em poucos segundos). Devolve o map messageId → resultado.
function aguardaResultados(elegiveis, ms = 25000) {
  const alvo = new Set(elegiveis.map(e => e.messageId));
  const ini = Date.now();
  let resultado = resultadoAnterior();
  while (Date.now() - ini < ms) {
    resultado = resultadoAnterior();
    if (alvo.size && alvo.size === resultado.filter(r => alvo.has(r.messageId)).length) break;
    sleepSync(1500);
  }
  const map = new Map();
  for (const r of resultado) map.set(r.messageId, r);
  return map;
}

function sleepSync(ms) {
  // Node >=16.7 tem Atomics.wait (síncrono); n8n container roda Node 22+.
  const sab = new Int32Array(new SharedArrayBuffer(4));
  Atomics.wait(sab, 0, 0, ms);
}

function buscaEmails() {
  const out = execFileSync("node", [FETCH, "--full", `--days=${DAYS}`], {
    encoding: "utf8", timeout: 60000,
  });
  return JSON.parse(out);
}

async function main() {
  // Bandeira do botão do widget: desligado, não busca e-mail nem enfileira.
  if (!automationLigada()) {
    console.log(JSON.stringify({ novos: 0, elegiveis: [], automacao: "desligada" }));
    return;
  }
  // IMAP desligado (2026-09-27): a vaga vem da coleta direta nos portais
  // (browser/coleta.py + coleta.js). O workflow do n8n pode continuar chamando
  // este script — ele sai aqui, sem tocar no e-mail.
  if (!emailLigado()) {
    console.log(JSON.stringify({ novos: 0, elegiveis: [], email: "desligado",
                                 fonte: "sites (coleta direta)" }));
    return;
  }
  let emails;
  try {
    emails = buscaEmails();
  } catch (e) {
    console.error("pipeline: busca IMAP falhou:", e.message);
    process.exit(3);
  }
  if (!emails || !emails.length) {
    console.log(JSON.stringify({ novos: 0, elegiveis: [] }));
    return;
  }
  const elegiveis = [];
  const resultados = [];
  for (const em of emails) {
    const idVaga = db.upsertVaga({ messageId: em.messageId, subject: em.subject, from: em.from, link: em.url, snippet: em.snippet });
    const analise = await classificar(em.corpo || em.snippet || "", em.subject || "");
    db.gravaAnalise(em.messageId, analise);
    db.proximaEtapa(idVaga, analise.aprovado ? "analisada_aprovada" : "analisada_recusada",
      analise.motivo || "");
    const r = { ...em, ...analise, idVaga };
    resultados.push(r);
    if (analise.aprovado) elegiveis.push(r);
  }
  enfileiraElegiveis(elegiveis);
  const sidecar = aguardaResultados(elegiveis);
  const elegiveisCompleto = elegiveis.map(e => {
    const s = sidecar.get(e.messageId);
    if (!s) return e;
    return {
      ...e,
      browser: {
        portal: s.portal || "",
        url: s.url || "",
        titulo: s.titulo || "",
        tem_botao: s.tem_botao === true,
        etapa: s.etapa || "",
        perguntas: s.perguntas || [],
        status: s.erro ? s.erro
          : s.enviado === true ? "candidatado ✅"
          : { ja_candidatado: "já candidatado", encerrada: "vaga encerrada",
              exige_questionario: "exige questionário",
              // bloqueadas na conferência da página (sidecar)
              regime_incompativel: "regime não é CLT (página)",
              exige_superior: "exige superior/certificação (página)",
              externa: "vaga externa (site da empresa)" }[s.etapa]
          || (s.tem_botao ? "pronta_para_candidatar" : s.etapa || "sem_botao"),
      },
    };
  });
  console.log(JSON.stringify({ novos: emails.length, elegiveis: elegiveisCompleto, resultados }));
}

if (require.main === module) {
  main().then(() => process.exit(0)).catch(e => { console.error("pipeline:", e.message); process.exit(1); });
}