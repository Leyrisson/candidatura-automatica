"use strict";
// reclassificar.js — reprocessa vagas JÁ lidas no IMAP cujo resultado veio de
// uma classificação errada.
//
// Por que existe (2026-09-27): entre 26/09 07:00 e 27/09 10:00 TODAS as
// classificações caíram no fallback por timeout do LocalAI, e o fallback exigia
// a palavra "CLT" — que os digests do InfoJobs/Catho não trazem. Resultado:
// 15 vagas recusadas por engano, nenhuma enfileirada. Reclassificar lê o corpo
// real de cada Message-ID no IMAP, repete a classificação com a heurística
// atual e reenfileira as que ficaram elegíveis.
//
// Seguro para rodar: grava etapa nova (reclassificada) sem apagar histórico,
// pula messageId que já tem resultado do sidecar, e aceita --dry-run.
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { ImapFlow } = require("/workflows/host/imap/node_modules/imapflow");
const { bodyText, linksDe } = require("/workflows/host/imap/fetch-inbox.js");
const db = require("./db.js");
const { classificar } = require("./classifica.js");
const { indexResultados } = require("./resultados.js");
const { urlsDaVaga } = require("./url-vaga.js");

const SECRETS = "/workflows/host/gmail-secrets.env";
const FILA = path.join(__dirname, "fila-vagas.jsonl");
const RES = path.join(__dirname, "resultado-vagas.jsonl");
const DRY = process.argv.includes("--dry-run");
// Janela de busca no Gmail: os messageIds do banco podem ser antigos e o
// search por header é confiável só dentro dela.
const DIAS = Number((process.argv.find(a => a.startsWith("--dias=")) || "=30").slice(7)) || 30;
const SO_IDS = (process.argv.find(a => a.startsWith("--ids=")) || "").slice(7)
  .split(",").map(s => s.trim()).filter(Boolean);

function envSecrets() {
  return Object.fromEntries(fs.readFileSync(SECRETS, "utf8").split("\n").filter(Boolean).map(l => {
    const i = l.indexOf("=");
    return [l.slice(0, i).trim(), l.slice(i + 1).trim()];
  }));
}

// Só as vagas cuja classificação foi feita no modo problemático: o motivo
// guardado tem "fallback" (o LLM não respondeu) — essas são as que precisam
// de novo julgamento. --ids= ignora o filtro (para reprocessar uma vaga só).
function alvos() {
  if (SO_IDS.length) {
    return db.listaVagas().filter(v => SO_IDS.includes(String(v.id)));
  }
  return db.listaVagas().filter(v => /fallback/i.test(v.motivo || ""));
}

async function corposPorMessageId(mids) {
  const env = envSecrets();
  const c = new ImapFlow({
    host: "imap.gmail.com", port: 993, secure: true,
    auth: { user: env.GMAIL_ADDR, pass: env.GMAIL_PASS }, logger: false,
  });
  const procurados = new Set(mids);
  const mapa = new Map();
  await c.connect();
  // mailboxOpen (e não getMailboxLock) é o que faz search/fetch funcionarem:
  // sem mailbox selecionada o search volta vazio sem erro. E no imapflow
  // 2.0.5 o fetch() devolve um AsyncGenerator — precisa `for await`, um
  // `for...of` normal itera zero vezes e parece não ter encontrado nada.
  await c.mailboxOpen("INBOX");

  // NÃO vale a pena buscar por header "message-id" (2026-09-27): o Gmail
  // devolvia UIDs de outra faixa, com o conteúdo de OUTRO e-mail (um digest
  // InfoJobs caía num e-mail de cupom da Uber), e nos UIDs certaininhos o
  // fetch voltava vazio. O Reliable é o contrário: um search por data, um
  // fetch em lote e casar pelo messageId que vem no ENVELOPE.
  const desde = new Date(Date.now() - DIAS * 864e5);
  const achados = await c.search({ since: desde });
  const uids = (achados || []).map(String);

  // Duas etapas de propósito: primeiro só o envelope (leve) para descobrir
  // quais UIDs são os procurados, e SÓ ENTESES o corpo deles. Buscar `source`
  // de 30 dias de caixa inteira dava timeout (2026-09-27).
  const alvo = new Map();
  for (const fatia of lotes(uids, 60)) {
    const metas = await c.fetch(fatia, { envelope: true }, { uid: true });
    for await (const m of metas) {
      const mid = String((m.envelope && m.envelope.messageId) || "");
      if (procurados.has(mid)) alvo.set(String(m.uid), mid);
    }
  }
  for (const fatia of lotes([...alvo.keys()], 10)) {
    const msgs = await c.fetch(fatia, { source: true }, { uid: true });
    for await (const m of msgs) {
      if (!m.source) continue;
      mapa.set(alvo.get(String(m.uid)), { corpo: bodyText(m.source), links: linksDe(m.source) });
    }
  }
  await c.logout().catch(() => {});
  return mapa;
}

function lotes(arr, n) {
  const out = [];
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n));
  return out;
}

async function main() {
  const vagas = alvos();
  if (!vagas.length) {
    console.log("nenhuma vaga com classificacao fallback para reprocessar");
    return;
  }
  console.log(`${DRY ? "[dry-run] " : ""}${vagas.length} vaga(s) para reclassificar`);
  const mids = [...new Set(vagas.map(v => v.message_id).filter(Boolean))];
  const achados = await corposPorMessageId(mids);

  const jaResolvidos = indexResultados(RES);
  const elegiveis = [];
  let reprocessadas = 0, semCorpo = 0;

  for (const v of vagas) {
    const achado = achados.get(v.message_id);
    const corpo = achado && achado.corpo;
    if (!corpo || corpo.length < 40) {
      semCorpo++;
      console.log(`  ? id=${String(v.id).padEnd(3)} e-mail fora da janela de ${DIAS} dias: ${(v.subject || "").slice(0, 50)}`);
      continue;
    }
    const a = await classificar(corpo, v.subject || "");
    reprocessadas++;
    console.log(`  ${a.aprovado ? "✔" : "✘"} ${(v.subject || "").slice(0, 52).padEnd(54)} ${a.regime || "?"}/${a.exigencia} ${a.motivo.slice(0, 70)}`);
    if (DRY) continue;
    db.gravaAnalise(v.message_id, a);
    db.proximaEtapa(v.id, a.aprovado ? "reclassificada_aprovada" : "reclassificada_recusada", a.motivo || "");
    if (a.aprovado) {
      // Uma linha pode trazer mais de uma vaga: uma entrada por URL.
      const urls = urlsDaVaga(corpo, v.link, achado.links);
      if (!urls.length) {
        console.log("      sem URL de vaga no e-mail, não enfileirado");
        continue;
      }
      for (const url of urls) {
        if (jaResolvidos.visto(v.message_id, url)) continue;
        elegiveis.push({
          messageId: v.message_id, idVaga: v.id, subject: v.subject, from: v.from_addr,
          url, local: a.local, regime: a.regime,
        });
      }
    }
  }

  console.log(`\nreprocessadas=${reprocessadas} sem_corpo=${semCorpo} elegiveis=${DRY ? "(dry-run)" : elegiveis.length}`);
  if (DRY || !elegiveis.length) return;

  // Mesmo formato que o pipeline escreve, para o watcher do sidecar ler igual.
  fs.appendFileSync(FILA, elegiveis.map(e => JSON.stringify(e)).join("\n") + "\n", "utf8");
  console.log(`enfileiradas ${elegiveis.length} em ${path.basename(FILA)}`);
  for (const e of elegiveis) console.log(`   ${e.idVaga} ${e.url || "(sem url)"}`);
}

// Primeiro link de portal no corpo; cai para o link do Gmail (mesma ordem do
// pipeline.js, que o sidecar consome em fila-vagas.jsonl).

main().then(() => process.exit(0)).catch(e => {
  console.error("reclassificar:", (e && (e.stack || e.message)) || JSON.stringify(e));
  process.exit(1);
});
