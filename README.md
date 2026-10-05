# Candidatura Automática de Vagas

Coleta vagas direto dos portais de emprego, **classifica pelos critérios do
candidato** e mantém tudo num SQLite. A coleta roda em navegador headless
(Playwright), a ponte entre as pontas é feita por arquivos, e nada depende de
API paga de portal.

![Node](https://img.shields.io/badge/Node-%3E%3D22.5%20node%3Asqlite-green) ![Python](https://img.shields.io/badge/Playwright-Firefox-blue)

## As três pontas

```
① coleta (browser/)      ② classificação (classifica.js)     ③ estado (db.js)
   headless, cards do        heurística barata primeiro,           SQLite com
   portal de vagas           LLM local só no desempate              as vagas + resultado
        │                            │                                │
        └──────────── jsonl (fila) ──┴────── jsonl (resultado) ─────┘
                                   ▲
                     ponte por arquivos: o container não fala com o host
```

## O problema de sempre: container não alcança host

O `n8n` roda em container e o browser roda no host. HTTP de container → host é
bloqueado por firewall. Em vez de afrouxar a regra, abrir exceção no iptables ou
subir um proxy só para isso, as duas pontas conversam por **arquivos num volume
compartilhado**:

- `fila-vagas.jsonl` — o pipeline enfileira as vagas elegíveis;
- `resultado-vagas.jsonl` — o sidecar devolve o que achou na página.

Esse padrão (`[[Orquestrador - n8n e Sidecar]]`) é o mesmo do sidecar HTTP: use
as duas mãos que você já tem.

## Critérios de classificação

A ordem importa, e o motivo está em cada arquivo:

1. **Heurística primeiro** (`heuristica()` em `classifica.js`). Grátis, e resolve
   o óbvio. Só o que sobra vai pro LLM.
2. **LLM local (LocalAI) como desempate**, com `LLM_TIMEOUT_MS=120000`.
3. **Regime (CLT) é tri-estado**, e isso foi um bug real:
   - `"CLT"` — o texto diz CLT;
   - `"incompativel"` — o texto diz PJ/temporário/estágio/freelancer → reprova;
   - `"?"` — o texto não diz nada → **segue**, e o sidecar confere na página.

   Exigir a string literal `"CLT"` reprovava 100% das vagas: os digests dos
   portais listam só cargo, empresa e cidade.

## Detalhes que custaram tempo

- **A área de TI não pode ser procurada no texto todo.** O card do portal inclui
  o nome da empresa, e `Operador de Logística` do `GRUPO SUPORTE` casava
  "suporte" e entrava como vaga de TI. Por isso a coleta direta passa o **cargo**
  separado, em `areaTexto`.
- **Um e-mail pode trazer várias vagas.** A linha
  `"Anacam contrata Auxiliar de Ti +1 novas vagas de Analista de Ti"` precisa
  virar uma entrada por URL, senão só a primeira seria aplicada.
- **Prefill do LLM é o custo, não a geração.** 12k chars ≈ 3,7k tokens ≈ 40 s
  só de prefill. Mandando para o modelo apenas as frases que falam dos critérios
  (limite `LLM_MAX_CHARS=1800`), a chamada cai para ~15 s.
- **`max_tokens=300` truncava o JSON.** O modelo fechava o objeto e abria outro;
  `finish_reason` voltava `length` e o texto ficava sem objeto fechável.
- **O botão é uma bandeira, não uma autorização.** `estado.js` lê
  `estado.json`; sem o arquivo — ou ilegível — a automação **não enfileira e o
  sidecar não aplica nada**. Na dúvida, o pior erro seria candidatar para vaga
  errada.

## Layout

| Arquivo | Papel |
|---|---|
| `browser/coleta.py` | raspa os cards das páginas de busca (InfoJobs) |
| `browser/sidecar.py` | abre a vaga, decide e preenche o formulário |
| `browser/perfil.json` | respostas do formulário por palavra-chave — **edite antes de usar** |
| `browser/alternar.py` | botão liga/desliga (escreve `estado.json`) |
| `browser/lock_perfil.py` | `flock` no perfil — duas coletas não brigam pelo Firefox |
| `classifica.js` | heurística + LLM local |
| `pipeline.js` | orquestra IMAP → classifica → enfileira (caminho de e-mail) |
| `coleta.sh` | coleta direta: browser → classifica → fila |
| `db.js` | schema SQLite (`vagas`, `candidaturas`, …) |
| `estado.js` | bandeira liga/desliga |
| `bench-modelos.js` | mede latência dos modelos antes de trocar o padrão |
| `resultados.js` | deduplicata uma vaga entre coletas (bridge no `estado.js`) |
| `mandar-telegram.py` | manda 1 mensagem por vaga e grava o mapa `message_id → vaga` |
| `gravar-respostas.py` | grava a resposta e separa o que é reaproveitável do que é só desta vaga |
| `monitorar.py` / `relatorio.py` | radar de vagas novas e o relatório diário |
| `browser/test_criterios_pagina.py` | testes do preenchimento (rodam sem browser) |
| `test-coleta-criterios.js` | testes dos critérios de classificação |

## Responder as perguntas pelo Telegram

Vaga com questionário travava em `exige_questionario` e ninguém via. Agora o
ciclo fecha sem sair do celular, e **a candidatura só sai depois do clique**:

1. `mandar-telegram.py` manda **uma mensagem por vaga** e guarda
   `telegram-vagas.json` com `message_id → url`. Uma por mensagem porque a
   Catho escreve `*` nos campos obrigatórios e, num texto só, os `*` se casam
   entre perguntas diferentes.
2. No telebot, a mensagem vira uma sessão: ele pergunta **uma por vez**,
   aceita `pula`, e no fim mostra tudo num resumo com os botões **Enviar** e
   **Descartar**.
3. `gravar-respostas.py` grava cada resposta em `browser/respostas-vaga.json`
   e decide o destino dela:

   | Tipo de resposta | Vai para |
   |---|---|
   | objetiva e curta (`Sim`, `Windows 11`, ` técnico`) | `perfil.json`, reaproveita nas próximas |
   | salário, pretensão, disponibilidade | só nesta vaga |
   | resposta aberta, texto longo, anexo, contato | só nesta vaga |

   É o que evita perguntar de novo o que você já respondeu dez vezes — sem
   learnar por accidento uma resposta que era específica daquela vaga.

4. O botão **Enviar** enfileira em `fila-vagas.jsonl` e o watcher processa.
   O telebot espera a linha aparecer em `resultado-vagas.jsonl` e só aí
   confirma.

Dois detalhes que custaram tempo:

- **O sidecar lê a resposta da vaga antes do `perfil.json`.** Sem isso, uma
  resposta dita no Telegram era sobrescrita pela regra genérica e a vaga ia
  para `revisar` mesmo com a resposta certa gravada.
- **`POST /vaga` não serve para candidatar.** Ele só *lê* a página e devolve o
  texto e os links; quem preenche e envia é o watcher, lendo a fila. Integrar
  o botão no endpoint errado dava "enviei" sem ter candidatado nada.

`browser/respostas-vaga.json` e `telegram-vagas.json` são estado de execução e
estão no `.gitignore`: contêm o que a pessoa respondeu e o mapa das mensagens.

## Instalação

```bash
python3 -m venv browser/.venv
browser/.venv/bin/pip install -r requirements.txt
browser/.venv/bin/playwright install firefox

# ligar a automação (o padrão é DESLIGADO)
python3 browser/alternar.py
```

O caminho de e-mail (`pipeline.js`) usa `imapflow` instalado no volume do n8n
(`/workflows/host/imap/node_modules`) e **não** precisa de nada aqui. A coleta
direta, que é o fluxo atual, é autocontida.

## Variáveis

| Variável | Padrão | Para quê |
|---|---|---|
| `LLM_URL` | `http://localai:8080/v1/chat/completions` | LLM local |
| `LLM_MODEL` | `qwen3.8-2b-q4` | modelo do desempate |
| `LLM_TIMEOUT_MS` | `120000` | tempo por chamada |
| `LLM_MAX_TOKENS` | `300` | **não baixar** (trunca o JSON) |
| `LLM_MAX_CHARS` | `1800` | trecho enviado ao LLM (o prefill domina) |
| `VAGAS_SEM_LLM` | — | `1` desliga o LLM no lote |
| `VAGAS_DB` | `../vagas.db` | caminho do SQLite |
| `VAGAS_ESTADO` | `./estado.json` | bandeira liga/desliga |
| `VAGAS_HEADLESS` | `1` | browser sem janela |

## ⚠️ Aviso

`browser/perfil.json` é um **modelo**, com valores fictícios: o nome do
candidato é `Fulano de Tal` e a faixa salarial é um exemplo. Preencha com os
seus antes de rodar — é ele que o sidecar digita nos formulários. As skills
(`firewall`, `kaspersky`, `acronis`…) são reais porque é o que dá utilidade
ao arquivo.

O `browser/perfil.json` também é lido em memória pelo sidecar. Se você editar
ele com outro processo, reinicie o `vagas-sidecar` — o cache só cai no boot.

Automatizar candidacyatura em portal é contra os termos de uso de vários deles.
Use no seu ritmo, com conta própria, e em volume humano.

## Requisitos

- Node ≥ 22.5 (para `node:sqlite`)
- Python 3.12+ com Playwright
- LocalAI (opcional — a heurística funciona sem ele)
