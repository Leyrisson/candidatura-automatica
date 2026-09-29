// Etapa 2 da coleta direta: lê a ponte `coleta-vagas.jsonl` (produzida por
// browser/coleta.py, que roda no host porque é quem tem o perfil do browser
// logado), passa cada vaga pelo MESMO classificador que o pipeline de e-mail
// usava e enfileira as aprovadas para o sidecar — que só candidata se a página
// confirmar os critérios.
//
// Por que um arquivo e não HTTP: o container do n8n não alcança
// 127.0.0.1:8788 do host (registrado na nota do projeto). A ponte é o volume
// compartilhado, igual à fila.
const fs = require("fs");
const path = require("path");
const { classificar } = require("./classifica.js");
const { automationLigada } = require("./estado.js");
const { indexResultados } = require("./resultados.js");

const COLETA = path.join(__dirname, "coleta-vagas.jsonl");
const FILA = path.join(__dirname, "fila-vagas.jsonl");
const RES = path.join(__dirname, "resultado-vagas.jsonl");

// Registro de passagem pelo classificador. Sem ele, cada rodada reprocessa as
// mesmas vagas e a fila incha com repetição.
const VISTOS = path.join(__dirname, "coleta-classificadas.json");

function linhas(arq) {
  try {
    return fs.readFileSync(arq, "utf8").split("\n").filter((l) => l.trim());
  } catch (e) {
    return [];
  }
}

function jsonSeguro(l) {
  try { return JSON.parse(l); } catch (e) { return null; }
}

function jaClassificadas() {
  const s = jaClassificadas._set || (jaClassificadas._set = new Set());
  if (s.size) return s;
  for (const k of linhas(VISTOS)) {
    const arr = jsonSeguro(k);
    if (Array.isArray(arr)) arr.forEach((x) => s.add(x));
  }
  return s;
}

function marcarClassificadas(chaves) {
  const s = jaClassificadas();
  for (const k of chaves) s.add(k);
  // Janela deslizante: não precisamos de histórico eterno, só do recente.
  const todas = [...s].slice(-800);
  fs.writeFileSync(VISTOS, JSON.stringify(todas), "utf8");
}

// Identidade da vaga na origem: o id do portal quando existe, senão a URL.
function chave(v) {
  return v.id ? `id:${v.id}` : `url:${v.url}`;
}

function messageIdDe(v) {
  return v.id ? `coleta:${v.id}` : `coleta:${v.url}`;
}

// A fila é append-only e o sidecar NÃO apaga o que processa (a identidade de
// "já resolvido" está no resultado-vagas.jsonl). Sem compactar, ela só cresce.
// Aqui é seguro reescrever porque, com o e-mail desligado, o ÚNICO escritor da
// fila é este script — o container do n8n não enfileira mais nada.
function compactarFila(feitos) {
  const atual = linhas(FILA).map(jsonSeguro).filter(Boolean);
  const pendentes = atual.filter((i) => {
    const mid = i.messageId || "";
    return mid ? !feitos.visto(mid, i.url) : false;
  });
  if (pendentes.length === atual.length) return 0;
  const tmp = `${FILA}.tmp`;
  fs.writeFileSync(tmp, pendentes.map((i) => JSON.stringify(i)).join("\n")
    + (pendentes.length ? "\n" : ""), "utf8");
  fs.renameSync(tmp, FILA);
  return atual.length - pendentes.length;
}

async function main() {
  if (!automationLigada()) {
    console.log(JSON.stringify({ coletadas: 0, enfileiradas: 0, automacao: "desligada" }));
    return;
  }

  const bruto = linhas(COLETA).map(jsonSeguro).filter(Boolean);
  if (!bruto.length) {
    const r0 = compactarFila(indexResultados(RES));
    console.log(JSON.stringify({ coletadas: 0, enfileiradas: 0, fila_removidas: r0 }));
    return;
  }

  const vistos = jaClassificadas();
  const feitos = indexResultados(RES);
  const naFila = new Set(linhas(FILA).map(jsonSeguro).filter(Boolean).map((i) => i.messageId));

  let jaVista = 0, jaAplicada = 0, reprovadas = 0;
  const enfileirar = [];

  for (const v of bruto) {
    const k = chave(v);
    if (vistos.has(k)) { jaVista++; continue; }
    vistos.add(k);

    const messageId = messageIdDe(v);
    if (feitos.visto(messageId, v.url) || naFila.has(messageId)) {
      jaAplicada++;
      continue;
    }

    // Texto do card como corpo, título como assunto: exatamente o formato que o
    // classificador já recebia do e-mail.
    const analise = await classificar(v.texto || "", v.titulo || "", { areaTexto: v.titulo || "", deFim: true });
    if (!analise.aprovado) { reprovadas++; continue; }

    naFila.add(messageId);
    enfileirar.push({
      messageId,
      subject: v.titulo || "",
      from: `${v.portal} (coleta direta)`,
      url: v.url,
      snippet: (v.texto || "").slice(0, 300),
      origem: v.origem || "coleta direta",
      idPortal: v.id || "",
      analise,
    });
  }

  if (enfileirar.length) {
    fs.appendFileSync(FILA, enfileirar.map((a) => JSON.stringify(a)).join("\n") + "\n", "utf8");
  }

  const removidas = compactarFila(feitos);
  console.log(JSON.stringify({
    coletadas: bruto.length,
    ja_vistas: jaVista,
    ja_aplicadas: jaAplicada,
    reprovadas,
    enfileiradas: enfileirar.length,
    fila_removidas: removidas,
  }));

  marcarClassificadas(bruto.map(chave));
}

main().then(() => process.exit(0)).catch((e) => {
  console.error("coleta:", e.message);
  process.exit(1);
});
