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

// Identidade da candidatura: a URL quando ela existe, o messageId só como
// reserva para resultado antigo sem url.
//
// A chave era `messageId + URL` composta (o par tinha que bater junto) e isso
// reprocessava vaga já resolvida sempre que o messageId mudava. Foi o que
// duplicou a 37382446 da Catho: a rodada manual gravou `rod2-05`, a coleta
// gravou `coleta:catho:37382446`, mesma URL, e o par nunca casou — a vaga foi
// lida e processada duas vezes.
//
// Por que a URL manda: ela é a identidade ESTÁVEL da vaga (o coletor a
// reconstrói do card), então reencontrar a mesma URL é reencontrar a mesma
// vaga, mesmo com messageId novo. E casar por messageId não pode ser o padrão
// porque um digest de e-mail traz VÁRIAS vagas com o mesmo messageId — aí a 2ª
// seria dada como resolvida e ignorada.
function indexResultados(caminho) {
  const mids = new Set();
  const urls = new Set();
  for (const r of lerResultados(caminho)) {
    const mid = String(r.messageId || "");
    if (mid) mids.add(mid);
    const u = norm(r.url);
    if (u) urls.add(u);
  }
  return {
    visto: (messageId, url) => {
      const u = norm(url);
      // Resultado antigo sem url: não dá para saber qual vaga era, então trata o
      // messageId inteiro como resolvido (evita candidatura duplicada).
      if (!u) return mids.has(String(messageId || ""));
      return urls.has(u);
    },
  };
}

module.exports = { lerResultados, indexResultados };
