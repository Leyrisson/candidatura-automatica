// Regressão 2026-09-27: o guard SINAL_DE_VAGA existia para barrar newsletter de
// e-mail, mas num card de página de busca todo card JÁ é vaga. Sem `deFim`, as
// vagas legítimas de TI caíam por não terem a palavra "vaga" no título.
const { classificar } = require("./classifica.js");

(async () => {
  const cartao = {
    areaTexto: "Analista de Redes Sênior",
    deFim: true,
  };
  const conteudo = "Analista de Redes Sênior 21 set 4,2 Empresa X Sao Paulo - SP "
    + "A combinar Ensino Tecnico Presencial";

  const casos = [
    ["card de portal, cargo de TI + SP (deve aprovar)",
      () => classificar(conteudo, "Analista de Redes Sênior", cartao), true],
    ["card de portal, cargo de TI + 100% remoto (deve aprovar)",
      () => classificar("Suporte Técnico N2 100% Remoto Innoque Vaga CLT A combinar "
        + "Ensino Medio PcD Home office", "Atendente de Suporte Técnico TI",
      { areaTexto: "Atendente de Suporte Técnico TI", deFim: true }), true],
    ["card de portal, cargo fora de area (deve recusar)",
      () => classificar("Operador de Logistica 25 set 4,2 GRUPO SUPORTE Cabreuva - SP "
        + "Ensino Fundamental Presencial", "Operador de Logística",
      { areaTexto: "Operador de Logística", deFim: true }), false],
    ["card de portal, exige ensino superior (deve recusar)",
      () => classificar("Analista de Infraestrutura de TI JR Sao Paulo Ensino Superior "
        + "Presencial CLT", "Analista de Infraestrutura De TI JR",
      { areaTexto: "Analista De Infraestrutura De TI JR", deFim: true }), false],
    ["card de portal, fora de SP e nao remoto (deve recusar)",
      () => classificar("Analista de Suporte Recife - PE A combinar Ensino Tecnico "
        + "Presencial CLT", "Analista de Suporte",
      { areaTexto: "Analista de Suporte", deFim: true }), false],
    ["e-mail de newsletter com TI e SP (deve recusar, sem deFim)",
      () => classificar("Estamos contratando para a area de TI em São Paulo. "
        + "Candidatos cv no portal.", "Newsletter TI"), false],
    ["e-mail de vaga de TI em SP (deve aprovar, sem deFim)",
      () => classificar("Vaga: Analista de Suporte Técnico em São Paulo - SP, "
        + "contratação CLT.", "Vaga Analista de Suporte"), true],
  ];

  let passou = 0;
  for (const [nome, fn, esperado] of casos) {
    const r = await fn();
    const ok = r.aprovado === esperado;
    if (ok) passou++;
    console.log(`${ok ? "ok  " : "FALHA"} ${nome}`);
    if (!ok) console.log(`       obtido: ${r.aprovado} | ${r.motivo}`);
  }
  console.log(`\n${passou}/${casos.length} casos ok`);
  process.exit(passou === casos.length ? 0 : 1);
})();
