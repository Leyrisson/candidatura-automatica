// resultado-vagas.jsonl é JSONL: um JSON por linha. Ler com JSON.parse no
// arquivo inteiro lança exceção em TODO caso, o chamador engolia o erro e
// devolvia [] — aí os mesmos messageIds voltavam para a fila e o sidecar
// candidava duas vezes para a mesma vaga (2026-09-27).
const fs = require("fs");

const norm = (u) => String(u || "").split("#")[0].split("?")[0];

function lerResultados(caminho) {
  let txt;
  try {
    txt = fs.readFileSync(caminho, "utf8");
  } catch (e) {
    return [];
  }
  const linhas = txt.split("\n").map((l) => l.trim()).filter(Boolean);
  const saida = [];
  for (const l of linhas) {
    try {
      saida.push(JSON.parse(l));
    } catch (e) {
      // linha truncada por escrita concorrente: descarta só ela
    }
  }
  return saida;
}

// Identidade da candidatura: messageId + URL. Um digest de e-mail pode trazer
// mais de uma vaga, e elas entram na fila com o mesmo messageId e URLs
// diferentes — chedar só por messageId (como o watcher fazia) ignorava a 2ª.
function indexResultados(caminho) {
  const porChave = new Set();
  const semUrl = new Set();
  for (const r of lerResultados(caminho)) {
    const mid = String(r.messageId || "");
    if (!mid) continue;
    const u = norm(r.url);
    if (u) porChave.add(`${mid}|${u}`);
    else semUrl.add(mid);
  }
  return {
    // Resultado antigo sem url gravada: não dá para saber qual vaga era, então
    // trata o messageId inteiro como já resolvido (evita candidatura duplicada).
    visto: (messageId, url) => semUrl.has(messageId)
      || porChave.has(`${messageId}|${norm(url)}`),
  };
}

module.exports = { lerResultados, indexResultados };
