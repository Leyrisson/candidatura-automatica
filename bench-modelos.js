#!/usr/bin/env node
// bench-modelos.js — compara os modelos do LocalAI com o prompt REAL do
// classificador (trechoParaLLM), medindo tempo e se o JSON volta completo.
const { trechoParaLLM, primeiroObjeto } = require("./classifica.js");

const SISTEMA = "/no_think\n"
  + "Voce e um triador de vagas. Responda APENAS JSON valido, sem markdown, sem crases, com as chaves:\n"
  + '{"local":"SP|Sorocaba|Remoto|","regime":"CLT|incompativel|?","areas":[],"exigencia":"nenhuma|superior|certificacao|ambas","aprovado":true|false,"motivo":"frase curta"}\n'
  + "Aprova (aprovado=true) quando TODAS valerem:\n"
  + "1. local for SP, Sorocaba, ou 100% remota.\n"
  + "2. regime NAO for \"incompativel\" (PJ, autonomo, temporario, estagio, freelancer). "
  + "Se o texto nao informar o regime, use \"?\" e NAO reprove por isso.\n"
  + "3. exigencia for \"nenhuma\".\n"
  + "4. areas contiver infraestrutura, suporte, analista de TI, analista de redes, SOC ou gerente de TI.\n"
  + "Se algum falhar, aprovado=false e explique no motivo.";

// Digest sintético no formato real que chega do InfoJobs (muito texto morto
// antes da vaga), para o prefill ser comparável ao de produção.
function digest(cargo, empresa, cidade, extra) {
  const lixo = "Rhf Talentos (4.53 estrelas) Têm Novas Vagas Para Você. Nome do Candidato, Empresas destacadas "
    + "acabam de publicar vagas interessantes para você. Ver novas vagas no portal, candidate-se pelo "
    + "site oficial, benefits,seal. ".repeat(60);
  return lixo + ` Vaga: ${cargo}. Empresa: ${empresa}. Local: ${cidade}. ${extra} `
    + "(hashut) ".repeat(60) + "Candidates should apply at the company website. ";
}

const CASOS = [
  ["digest gerente SP", digest("Gerente De Ti", "BASSH", "São Paulo", "Regime: CLT. Ensino médio completo."), "Novas Vagas de Gerente De Ti"],
  ["digest suporte SP", digest("Analista de Suporte Técnico", "Gmud Tecnologia", "São Paulo - SP", "Contratação CLT. Sem experiência."), "Vagas hoje de Analista De Suporte Técnico"],
  ["digest PJ", digest("Analista de Ti", "Alpina", "São Paulo", "Contratação como PJ. Remoto parcial."), "Vagas de Analista de Ti"],
  ["digest exige superior", digest("Analista de Redes", "Rede X", "Sorocaba", "Regime CLT. Exige ensino superior completo."), "Vagas de Analista de Redes"],
];

const MODELOS = process.argv.slice(2).length ? process.argv.slice(2)
  : ["qwen3.8-2b-q4", "llama-3.2-3b-instruct:q4_k_m"];

const URL = process.env.LLM_URL || "http://127.0.0.1:8090/v1/chat/completions";

async function uma(modelo, nome, conteudo, assunto) {
  const trecho = trechoParaLLM(conteudo, assunto);
  const t0 = Date.now();
  try {
    const res = await fetch(URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: modelo, temperature: 0.1, max_tokens: 300,
        messages: [
          { role: "system", content: SISTEMA },
          { role: "user", content: `Assunto: ${assunto}\n\nConteudo:\n${trecho}` },
        ],
      }),
      signal: AbortSignal.timeout(300000),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    const d = await res.json();
    const c = d.choices[0];
    const p = primeiroObjeto(c.message.content);
    const dt = ((Date.now() - t0) / 1000).toFixed(1);
    const u = d.usage || {};
    return `${dt}s in=${u.prompt_tokens} out=${u.completion_tokens} finish=${c.finish_reason} `
      + `json=${p ? "OK" : "FALHOU"} ${p ? `aprovado=${p.aprovado} local=${p.local} regime=${p.regime} exig=${p.exigencia}` : ""}`;
  } catch (e) {
    return `${((Date.now() - t0) / 1000).toFixed(1)}s ERRO ${e.name}: ${String(e.message).slice(0, 60)}`;
  }
}

(async () => {
  for (const m of MODELOS) {
    console.log("\n=== " + m + " ===");
    let soma = 0;
    for (const [nome, corpo, assunto] of CASOS) {
      const r = await uma(m, nome, corpo, assunto);
      soma += parseFloat(r);
      console.log("  " + (nome + "                    ").slice(0, 22) + r);
    }
    console.log("  media: " + (soma / CASOS.length).toFixed(1) + "s");
  }
})();
