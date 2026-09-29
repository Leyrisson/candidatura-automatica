// Escolha da URL da vaga. Compartilhado por pipeline.js e reclassificar.js
// para os dois não divergirem na hora de enfileirar.
//
// A regra que importa: tem que ser a PÁGINA DA VAGA. O digest InfoJobs vem
// cheio de link de empresa/tracking, e o fallback mail.google.com/
// #all/rfc822msgid= exige sessão Google logada no navegador do sidecar — sem
// isso ele cai no accounts.google.com e a vaga morre (2026-09-27). LinkedIn
// Notificação também entrava, porque "linkedin" estava na lista de portais sem
// exigir forma de página de vaga.

const PAGINA_DE_VAGA = [
  { portal: "infojobs", padrao: /infojobs\.com\.br\/(vaga-de-emprego|vaga|emprego)\//i },
  { portal: "gupy", padrao: /gupy\.io\/(vagas|vaga)\//i },
  { portal: "catho", padrao: /catho\.com\.br\/vagas?\//i },
  { portal: "vagas.com", padrao: /vagas\.com\/(vaga|vagas)\//i },
  { portal: "glassdoor", padrao: /glassdoor\.com\.br\/(job|vaga|emprego)\//i },
  { portal: "linkedin", padrao: /linkedin\.com\/jobs\/view\//i },
];

const LINK_RUIM = /mail\.google\.com|accounts\.google\.com|\/analytics\/|\/comm\/|\/feed\/|\/messaging\/?$|\/in\/[^/]*$|unsubscribe|\/tracking/;

function extraiUrls(texto) {
  return (texto || "").match(/https?:\/\/[^\s"'<>)\]]+/g) || [];
}

// Página de vaga do InfoJobs: /vaga-de-<slug>__<id>.aspx — o slug é só SEO,
// então dá para montar a partir do id. Os ids saem dos botões do digest:
//   a.aspx?xidua=..&iv=9240731&idc=..&idl=..&ivs=9240731,9240454&iu=25
// "iv" é a vaga e "ivs" é a lista de vagas do e-mail. O "a.aspx" em si
// redireciona para a HOME do portal (testado em 27/09/2026), então não serve.
const INFOJOBS_PAGINA = /infojobs\.com\.br\/vaga-de-[^"'\s]*?__(\d+)\.aspx/i;
// Qualquer URL do infojobs com iv=<id>: o "a.aspx" dos botões do digest
// (a.aspx?xidua=..&iv=9240731&ivs=...) e o detailvacancy/about.aspx?iv=..
const INFOJOBS_IV = /infojobs\.com\.br\/[^"'\s]*?[?&]iv=(\d+)/i;

// Uma linha pode trazer MAIS de uma vaga: devolve todas as páginas de vaga
// distintas, para o pipeline enfileirar uma por vez.
function urlsDaVaga(texto, alternativa, links) {
  const brutos = [
    ...extraiUrls(texto),
    ...(Array.isArray(links) ? links : []),
  ].filter(u => !LINK_RUIM.test(u));

  const achadas = [];
  const adiciona = u => {
    const id = (u.match(INFOJOBS_PAGINA) || u.match(INFOJOBS_IV) || [])[1];
    if (id) achadas.push(`https://www.infojobs.com.br/vaga-de-vaga__${id}.aspx`);
  };
  brutos.forEach(adiciona);

  if (!achadas.length) {
    for (const { padrao } of PAGINA_DE_VAGA) {
      const achada = brutos.find(u => padrao.test(u));
      if (achada) { achadas.push(achada); break; }
    }
  }
  if (!achadas.length && /^https?:/.test(alternativa || "") && !LINK_RUIM.test(alternativa)) {
    achadas.push(alternativa);
  }
  return [...new Set(achadas)];
}

// Aceita o texto do e-mail E a lista de hrefs: a URL da vaga vive no href do
// HTML e o bodyText() remove as tags, então buscar só no texto nunca encontra
// nada. `alternativa` é o link do Gmail (ruim) ou o campo url do registro.
function urlDaVaga(texto, alternativa, links) {
  return urlsDaVaga(texto, alternativa, links)[0] || "";
}

module.exports = { PAGINA_DE_VAGA, LINK_RUIM, urlDaVaga, urlsDaVaga };
