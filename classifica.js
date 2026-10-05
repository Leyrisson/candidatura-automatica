"use strict";
// Classifica uma vaga pelos critérios do usuário usando LLM local (LocalAI).
// Critérios: local em SP/Sorocaba/100% remota · regime CLT · sem exigência de
// ensino superior/certificação · áreas de infra/suporte/analista TI/redes/SOC/
// gerente de TI.
//
// REGIME (2026-09-27): os digests do InfoJobs/Catho — que é o que chega por
// e-mail — listam só cargo, empresa e cidade. A palavra "CLT" NÃO aparece, e o
// classificador exigia a string literal: resultado, 100% de recusa. O regime
// passou a ser verificado na PÁGINA da vaga, pelo sidecar (que já abre a URL e
// decide se candidata). Aqui o regime vira tri estado:
//   "CLT"          — o e-mail diz CLT
//   "incompativel" — o e-mail diz PJ/temporário/estágio/freelancer → reprova aqui
//   "?"            — o e-mail não diz nada → segue e o sidecar confere na página

const LLM_URL = process.env.LLM_URL || "http://localai:8080/v1/chat/completions";
const MODEL = process.env.LLM_MODEL || "qwen3.8-2b-q4";
// Timeout da chamada. Medido em 2026-09-27 com o modelo aquecido: 43-52s para
// prefill de 5,4k tokens. 90s não tinha folga e 180s mascarava o problema real
// (prompt grande demais), então o prompt foi enxugado e o timeout caiu.
const LLM_TIMEOUT_MS = Number(process.env.LLM_TIMEOUT_MS || 120000);
// 300 tokens truncavam o JSON: o modelo fechava o objeto e abria outro, o
// finish_reason voltava "length" e o texto ficava sem objeto fechável.
const LLM_MAX_TOKENS = Number(process.env.LLM_MAX_TOKENS || 300);
// Limite do TRECHO ENVIADO AO LLM (não do e-mail inteiro). Prefill dominava o
// custo: 12k chars ≈ 3,7k tokens ≈ 40s só de prefill. Mandando só as frases
// que falarem dos critérios, cai para ~600 tokens e a chamada inteira fica em
// ~15s. O texto completo continua sendo usado pela heurística, que é gratis.
const LIMITE_CARACTERES = Number(process.env.LLM_MAX_CHARS || 1800);

const AREAS = ["infraestrutura", "infra", "suporte", "analista de ti", "analista de redes", "soc", "gerente de ti"];

// Sinais de regime INCOMPATÍVEL com CLT. Ausência de "CLT" não reprova (o
// digest não informa), mas a presença explícita de PJ/temporário reprova.
const REGIME_RUIM = /(pessoa\s*[jí]rica|\bpj\b|prestador\s*de\s*servi|contrata[çc][ãa]o\s*como\s*pj|aut[ôo]nomo|freela(ncer|ance|nte)|tempor[áa]ri[oa]|temporario|contrato\s*determinado|est[áa]gi[óo]|estagi[áa]ri[oa]|aprendiz|estágio|estagio)/i;
const REGIME_CLT = /(\bclt\b|celetista|consolida[cç][aã]o\s*das\s*leis\s*do\s*trabalho|carteira\s*assinada|regime\s*efetivo|efetivo\s*-\s*clt)/i;

// Heurística barata antes do LLM (evita chamar LLM em coisa óbvia).
// `areaTexto` = onde procurar a ÁREA de TI. O padrão é o texto todo (e-mail,
// onde o cargo vem no assunto), mas a coleta direta passa só o cargo: no card
// do portal o texto inclui o NOME DA EMPRESA, e "Operador de Logística" do
// GRUPO SUPORTE casava "suporte" e entrava como vaga de TI (2026-09-27).
function heuristica(texto, areaTexto) {
  const t = (texto || "").toLowerCase().replace(/\s+/g, " ");
  const a = String(areaTexto === undefined ? texto : areaTexto || "")
    .toLowerCase().replace(/\s+/g, " ");
  const ehRemoto = /(100\s*%\s*remot|remoto\s*(de\s*cualquier lugar|total|global)|home\s*office|remoto\b)/.test(t);
  const ehSP = /(s[aã]o paulo|sorocaba|sp\b|regi[aã]o metropolitana)/.test(t);
  const regimeRuim = REGIME_RUIM.test(t);
  const ehCLT = REGIME_CLT.test(t) && !regimeRuim;
  const nega = /(sem\s+((ensino\s+)?superior|gradua|curso\s+superior|certifica|ccna|comptia)|n[aã]o\s+(exig|é exigido|é obrigat|precisa de superior|requer)|dispens|opcional|desej)/;
  const exigeSuperior = /(ensino superior|curso superior|gradua[cç][aã]o|superior completo|bacharel|t[eê]cnologo|licenciatura|n[íí]vel superior|forma[cç][aã]o em(\s|$)|superior em)/.test(t) && !nega.test(t);
  const exigeCert = /(certifica[cç][aã]o|ccna|comptia)/.test(t) && !nega.test(t);
  const exigeObrigatorio = /obrigat[óo]rio/.test(t) && !nega.test(t);
  const semSuperior = !exigeSuperior;
  const ehArea = AREAS.some(x => a.includes(x)) || /(infraestrutura|redes|noc|soc|helpdesk|mesa de (servi|ajuda)|mesa de servi|support|sysadmin|devops|gerente de ti|gestor de ti|coordena[dç]or de ti)/.test(a);
  // "?" = o e-mail não informa o regime. Não reprova por isso: quem confirma
  // é a página da vaga (sidecar). Ver comentário do REGIME no topo do arquivo.
  const regime = regimeRuim ? "incompativel" : ehCLT ? "CLT" : "?";
  return { ehRemoto, ehSP, ehCLT, regime, regimeRuim, semSuperior, ehArea, exigeSuperior, exigeCert, exigeObrigatorio };
}

// Trecho ENXUTO para o LLM: das frases do e-mail, só as que tocam nos critérios.
// O prefill é o gargalo (4 tok/s de saída e prefill linear no tamanho), então
// mandar o digest inteiro custava ~40s sem ganhar nada. Mantém a cabeça e a
// cauda do texto se nada for encontrado, para não devolver vazio.
const RELEVANTE = /(s[aã]o\s*paulo|sorocaba|sp\b|remoto|home\s*office|clt|celetista|pessoa\s*[jí]rica|\bpj\b|aut[ôo]nomo|freela|tempor[áa]ri|est[áa]gi|efetivo|regime|contrata|suporte|infraestrutura|infra\b|analista|redes|\bsoc\b|help|desk|socorro|gerente\s*de\s*ti|gestor\s*de\s*ti|tecn[óo]log|sal[áa]ri|benef[íi]cio|ensino\s*(m[ée]dio|fundamental|superior)|curso|gradua|experi[êe]ncia)/i;

function trechoParaLLM(texto, assunto) {
  const t = (texto || "").replace(/\s+/g, " ").trim();
  const pedacos = t.match(/[^.!?;]*(?:\d{2}\s*[-–]\s*)?[^.!?;]*[.!?;]?/g) || [t];
  const úteis = [];
  for (const p of pedacos) {
    const limpo = p.trim();
    if (limpo && RELEVANTE.test(limpo)) úteis.push(limpo);
    if (úteis.join(" ").length >= LIMITE_CARACTERES) break;
  }
  const corpo = úteis.join(" ").slice(0, LIMITE_CARACTERES);
  return corpo.length >= 80 ? corpo : t.slice(0, LIMITE_CARACTERES);
}

// Extrai o PRIMEIRO objeto JSON completo de uma resposta possivelmente
// truncada ou com sobra. O regex guloso /\{[\s\S]*\}/ estourava quando o modelo
// fechava o objeto e abria outro (`[{...},\n{...}` sem fim): o match ia do
// primeiro `{` ao último `}` e JSON.parse quebrava. Aqui o scan balança as
// chaves e ignora `{`/`}` que estejam dentro de string.
function primeiroObjeto(texto) {
  const t = String(texto || "");
  const inicio = t.indexOf("{");
  if (inicio < 0) return null;
  let profundidade = 0, emString = false, escapado = false;
  for (let i = inicio; i < t.length; i++) {
    const ch = t[i];
    if (emString) {
      if (escapado) escapado = false;
      else if (ch === "\\") escapado = true;
      else if (ch === '"') emString = false;
      continue;
    }
    if (ch === '"') emString = true;
    else if (ch === "{") profundidade++;
    else if (ch === "}") {
      profundidade--;
      if (profundidade === 0) {
        const cand = t.slice(inicio, i + 1);
        try { return JSON.parse(cand); } catch { return null; }
      }
    }
  }
  return null; // truncado no meio: sem objeto completo
}

async function chamaLLM(conteudo, assunto) {
  const system = "/no_think\n"
    + "Voce e um triador de vagas. Responda APENAS JSON valido, sem markdown, sem crases, com as chaves:\n"
    + '{"local":"SP|Sorocaba|Remoto|","regime":"CLT|incompativel|?","areas":[],"exigencia":"nenhuma|superior|certificacao|ambas","aprovado":true|false,"motivo":"frase curta"}\n'
    + "Aprova (aprovado=true) quando TODAS valerem:\n"
    + "1. local for SP, Sorocaba, ou 100% remota.\n"
    + "2. regime NAO for \"incompativel\" (PJ, autonomo, temporario, estagio, freelancer). "
    + "Se o texto nao informar o regime, use \"?\" e NAO reprove por isso.\n"
    + "3. exigencia for \"nenhuma\".\n"
    + "4. areas contiver infraestrutura, suporte, analista de TI, analista de redes, SOC ou gerente de TI.\n"
    + "Se algum falhar, aprovado=false e explique no motivo.";
  const corpo = trechoParaLLM(conteudo, assunto);
  try {
    const res = await fetch(LLM_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: MODEL,
        messages: [
          { role: "system", content: system },
          { role: "user", content: `Assunto: ${assunto}\n\nConteudo:\n${corpo}` },
        ],
        max_tokens: LLM_MAX_TOKENS,
        temperature: 0.1,
      }),
      signal: AbortSignal.timeout(LLM_TIMEOUT_MS),
    });
    if (!res.ok) throw new Error(`LocalAI respondeu HTTP ${res.status}`);
    const data = await res.json();
    const texto = data.choices?.[0]?.message?.content || "";
    const parsed = primeiroObjeto(texto);
    if (!parsed) {
      const finish = data.choices?.[0]?.finish_reason;
      throw new Error(`LocalAI devolveu texto sem objeto JSON (finish_reason=${finish})`);
    }
    if (!Array.isArray(parsed.areas)) parsed.areas = [];
    return parsed;
  } catch (e) {
    // Fallback: heurística pura quando o LLM falhar. O regime "?" NÃO reprova
    // (quem confirma é a página, no sidecar); só "incompativel" reprova aqui.
    // O motivo distingue timeout de erro HTTP: as causas são bem diferentes
    // (LocalAI lento x LocalAI fora/arruinado) e o usuário precisa saber qual.
    const h = heuristica(`${assunto} ${conteudo}`);
    const localOk = h.ehRemoto || h.ehSP;
    const aprovado = localOk && !h.regimeRuim && h.ehArea && !h.exigeSuperior && !h.exigeCert;
    const qual = e.name === "TimeoutError" || e.name === "AbortError"
      ? `timeout do LocalAI (>${Math.round(LLM_TIMEOUT_MS / 1000)}s)`
      : e.message;
    return {
      local: h.ehRemoto ? "Remoto" : h.ehSP ? "SP" : "",
      regime: h.regime,
      areas: AREAS.filter(a => (corpo || "").toLowerCase().includes(a)),
      exigencia: h.exigeSuperior && h.exigeCert ? "ambas" : h.exigeSuperior ? "superior" : h.exigeCert ? "certificacao" : "nenhuma",
      aprovado,
      motivo: `fallback heuristica (${qual})`,
    };
  }
}

// Heurística primeiro, LLM só para desempatar (2026-09-27).
//
// Medição real com o prompt do classificador (bench-modelos.js):
//   qwen3.8-2b (Q8_0, CPU)  -> 55-75s por e-mail
//   llama-3.2-3b (Q4_K_M)   -> 15-95s por e-mail
// Os DOIS aprovaram a vaga "contratação como PJ" e a vaga "exige ensino
// superior completo" (aprovado=true, exigência=nenhuma) e ambos devolveram
// `local` copiando o enum ("SP|Sorocaba|Remoto") em vez de escolher um valor.
// Ou seja: o LLM é caro E errra justamente nos critérios que decides se a
// candidatura sai. A heurística acerta esses casos com regex em ~0ms.
//
// Então: a heurística decide. O LLM só entra quando a heurística NÃO consegue
// fechar o critério (reprovação sem motivo claro, ou área ambígua) — e o que
// o LLM Says nunca reprova por só: a autoridade final do regime e da exigência
// é a PÁGINA da vaga, conferida pelo sidecar antes de aplicar.
// Pistas de que o texto FALA de locality/remoto mas a heurística não fechou um
// valor limpo — é o caso em que o LLM ainda pode lucrar algo. Sem nenhuma pista
// (e-mail de marketing, notícia, curso), não há o que desempatar: reprovar sem
// gastar 60-90s de CPU com o LLM seria desperdício.
const PISTA_LOCAL = /(todo\s*o?\s*brasil|brasil|interior|interior\s*de|minas|gerais|rio\s*de\s*janeiro|curitiba|porto\s*alegre|belo\s*horizonte|s[ãa]o\s*paulo|sorocaba|sp\b|\bremoto\b|home\s*office|qualquer\s*cidade|qualquer\s*lugar)/i;
// Tenta de TI genérica sem bater nas áreas da lista: a heurística pode ter
// errado o rótulo da área, e aí o LLM ajuda a mapear.
const PARECE_TI = /\b(ti\b|t\.\s*i\.|tecnologia\s+da\s+informa|inform[áa]tica|help\s*desk|service\s*desk|mesa\s*de\s*(servi|ajuda)|atendimento|call\s*center|infra|redes|seguran[çc]a|cyber|sec\b)/i;

// Sinal de que o e-mail é REALMENTE sobre uma vaga. Sem isso, newsletter de
// curso, convite de webinar e aviso de plataforma que citam "São Paulo" e
// "suporte" (ou "Scrum") passavam pela heurística e entravam na fila — foi o
// que aprovou "Convite: Entenda por que Scrum não é sinônimo de agilidade!"
// (LinkedIn Learning) no reprocessamento de 2026-09-27.
// Só intenção explícita de vaga conta: "suporte"/"analista"/"carreira" são
// palavras genéricas demais (a área já é checada à parte) e ficam de fora.
const SINAL_DE_VAGA = /(vaga|vagas|oportunidade|candidat|inscrev|inscri[çc][ãa]o|curriculo|curr[íi]culo|estamos\s+contratando|we\s+are\s+hiring|trabalhe\s+conosco|recrut|seletiv|entrevista|processo\s+seletivo|vacanc)/i;

// `opts.areaTexto` limita onde a área de TI é procurada (a coleta direta passa
// só o cargo). Sem ele, vale o texto inteiro — comportamento do e-mail.
async function classificar(conteudo, assunto, opts) {
  const areaTexto = opts && opts.areaTexto !== undefined ? opts.areaTexto : conteudo;
  const texto = `${assunto} ${conteudo}`;
  const h = heuristica(texto, areaTexto);
  const localOk = h.ehRemoto || h.ehSP;
  const areas = AREAS.filter(a => String(areaTexto || "").toLowerCase().includes(a));
  // `opts.deFim`: o texto vem de uma PÁGINA DE RESULTADOS de busca, onde todo
  // card já é uma vaga. O SINAL_DE_VAGA existe para barrar newsletter/promoção
  // do e-mail; num card de portal ele só reprova por falta da palavra "vaga" no
  // título (2026-09-27: "Analista de Redes Sênior" e "Atendente de Suporte
  // Técnico TI" caíram assim). Local, área, regime e exigência continuam valendo.
  const ehVaga = (opts && opts.deFim) ? true : SINAL_DE_VAGA.test(texto);

  // Dúvida real = o texto cita locality mas ambígua, OU fala de TI sem cair
  // nas áreas da lista. Recusa óbvia (nem local, nem TI) NÃO vai ao LLM.
  const duvidoso = (!localOk && PISTA_LOCAL.test(texto)) || (!h.ehArea && PARECE_TI.test(texto));
  if (!duvidoso || !ehVaga) {
// 2026-10-03, regra do dono: a falta de ensino superior/certificação
      // NÃO reprova aqui. A decisão é do FORMULÁRIO (campo obrigatório), no
      // sidecar (`_campo_formacao_obrigatorio`), porque menção no card é
      // boilerplate: a maioria dos cards do InfoJobs traz "Ensino Superior" na
      // listagem sem que o formulário cobre o campo. Reprovar aqui descartava
      // ~13% do volume (24 de 179 cards na medição) por um requisito que
      // ninguém pergunta. A informação continua sendo enviada adiante, em
      // `exigencia`, para o sidecar conferir no formulário.
      const motivo = !ehVaga ? "e-mail sem sinal de vaga (newsletter/convite/aviso)"
        : !localOk ? "local fora de SP/Sorocaba/remoto"
        : !h.ehArea ? "area fora das áreas de TI pedidas"
        : h.regimeRuim ? `regime incompativel no e-mail (${h.regime})`
        : h.exigeSuperior || h.exigeCert ? "exige ensino superior/certificacao (conferir no formulario)"
        : "local e area ok; regime sera conferido na pagina da vaga";
      return {
        local: h.ehRemoto ? "Remoto" : h.ehSP ? "SP" : "",
        regime: h.regimeRuim ? "incompativel" : (h.ehCLT ? "CLT" : "?"),
        areas,
        exigencia: h.exigeSuperior && h.exigeCert ? "ambas" : h.exigeSuperior ? "superior" : h.exigeCert ? "certificacao" : "nenhuma",
        aprovado: ehVaga && localOk && h.ehArea && !h.regimeRuim,
        motivo: `heuristica (${motivo})`,
        fonte: "heuristica",
      };
  }

  // Lote grande (coleta direta) desliga o desempate: com 120s de timeout por
  // vaga, 80 vagas inconclusivas travam o coletor por horas. A heurística é a
  // primária por decisão de 2026-09-27; o LLM só existe para volume baixo.
  if (process.env.VAGAS_SEM_LLM === "1") {
    const Areas = areas;
    return {
      local: h.ehRemoto ? "Remoto" : h.ehSP ? "SP" : "",
      regime: h.regimeRuim ? "incompativel" : (h.ehCLT ? "CLT" : "?"),
      areas: Areas,
      exigencia: h.exigeSuperior && h.exigeCert ? "ambas" : h.exigeSuperior ? "superior" : h.exigeCert ? "certificacao" : "nenhuma",
      aprovado: false,
      motivo: "heuristica inconclusiva (desempate por LLM desativado no lote)",
      fonte: "heuristica",
    };
  }

  const llm = await chamaLLM(conteudo, assunto);
  // O LLM pode DESTRANCAR uma reprovação, nunca aprovar: os testes de
  // 2026-09-27 approvals em PJ e "exige superior", então a confiança é
  // assimétrica. O regime "incompativel" visto pela heurística também segura.
  const localOkL = llm.local === "SP" || llm.local === "Sorocaba" || llm.local === "Remoto" || h.ehSP || h.ehRemoto;
  const llmAprova = llm.aprovado === true
    && ehVaga
    && localOkL
    && llm.exigencia === "nenhuma"
    && llm.regime !== "incompativel"
    && !h.regimeRuim
    && (Array.isArray(llm.areas) && llm.areas.length > 0 || h.ehArea);
  return {
    local: llm.local && llm.local.length <= 20 ? llm.local : (h.ehRemoto ? "Remoto" : h.ehSP ? "SP" : ""),
    regime: h.regimeRuim ? "incompativel" : (llm.regime === "CLT" ? "CLT" : h.ehCLT ? "CLT" : "?"),
    areas: Array.isArray(llm.areas) && llm.areas.length ? llm.areas : areas,
    exigencia: llm.exigencia || "nenhuma",
    aprovado: llmAprova,
    motivo: `llm desempate (${llm.motivo || "sem motivo"})`,
    fonte: "llm",
  };
}

module.exports = { classificar, chamaLLM, heuristica, primeiroObjeto, trechoParaLLM, AREAS };

// Teste: node classifica.js "texto da vaga" "assunto"
if (require.main === module) {
  const conteudo = process.argv[2] || "";
  const assunto = process.argv[3] || "";
  classificar(conteudo, assunto).then(r => console.log(JSON.stringify(r, null, 2)));
}
