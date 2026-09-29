// Bandeira liga/desliga da automação. É o que o botão do widget escreve
// (browser/alternar.py). O padrão é DESLIGADO: sem o arquivo — ou com ele
// ilegível — a automação não enfileira e o sidecar não aplica nada, porque na
// dúvida o pior erro é candidatar para vaga errada.
const fs = require("fs");
const path = require("path");

const ARQ = process.env.VAGAS_ESTADO || path.join(__dirname, "estado.json");

function automationLigada() {
  try {
    const d = JSON.parse(fs.readFileSync(ARQ, "utf8"));
    return d.ativo === true;
  } catch (e) {
    return false;
  }
}

// Fonte de e-mail. O usuário pediu para parar de verificar o e-mail (2026-09-27):
// a coleta passa a vir direto dos portais. independente de `ativo` — dá para
// manter a coleta direta ligada com o IMAP desligado.
function emailLigado() {
  try {
    const d = JSON.parse(fs.readFileSync(ARQ, "utf8"));
    return d.email === true;
  } catch (e) {
    return false;
  }
}

module.exports = { ARQ, automationLigada, emailLigado };
