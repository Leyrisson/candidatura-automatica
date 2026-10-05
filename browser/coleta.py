#!/usr/bin/env python3
"""Coleta vagas DIRETO dos portais — sem e-mail.

Roda no HOST (é quem tem o perfil do Firefox logado) e escreve uma ponte em
`coleta-vagas.jsonl`, que o `coleta.js` classifica e enfileira para o sidecar.

Por que um arquivo e não HTTP: a nota do projeto registra que o container do
n8n não consegue falar com `127.0.0.1:8788` do host (a ponte é o volume
compartilhado). A Collected-via-arquivo mantém a mesma arquitetura que a fila já usa.

Cada portal é uma função `_coleta_infojobs(ctx)`. Para entrar na fila, a vaga
AINDA precisa passar em `classifica.js` (área/regime) e na conferência de
página do sidecar — aqui a única responsabilidade é conseguir a lista.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
import sidecar as S  # noqa: E402
from lock_perfil import perfil_exclusivo  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
BASE = Path(__file__).resolve().parent.parent
SAIDA = BASE / "coleta-vagas.jsonl"
VISTOS = BASE / "coleta-vistos.json"
# A busca do InfoJobs entrega 20 cards no DOM e NÃO tem paginação: `&page=N`
# devolve a mesma lista, não existe container com scroll próprio nem lazy-load
# (medido em 2026-10-02: 20 cards, scroll x3 e `&page=2/3` sem card novo). O
# corte em 12 descartava 8 cards que já estavam carregados — de graça. Ler os 20
# não custa tempo de rede, porque a página já foi carregada inteira.
MAX_POR_TERMO = 20
PAUSA = 1.5                # segundos entre buscas — não martela o portal

# (palavra, provincia) — provincia=8 é SP. Sem provincia, é busca por palavra
# (serve para Sorocaba, que não é capital).
BUSCAS = [
    ("analista de suporte", 8),
    ("suporte de ti", 8),
    ("analista de ti", 8),
    ("infraestrutura de ti", 8),
    ("redes de computacao", 8),
    ("seguranca da informacao soc", 8),
    ("gerente de ti", 8),
    ("suporte de informatica", 8),
    ("analista de infraestrutura", 8),
    ("help desk", 8),
    ("ti sorocaba", None),
    ("suporte sorocaba", None),
]

# Buscas NACIONAIS (decisão 2026-10-03). Com `provincia=8` a busca só devolve
# SP, e o que mais aparece hoje no portal é vaga "Todo Brasil" com "Home
# office" — que o filtro de provincialia escondia. Sem provincia, o classificador
# continua sendo o filtro: `classifica.js` só aprova quando o texto diz remoto
# ou SP/Sorocaba (`localOk = ehRemoto || ehSP`), então vaga presencial de outro
# estado é reprovada por local e nunca chega na fila.
#
# São os mesmos termos de cima, sem provincia: repetir o termo pega o recorte
# nacional da busca, que é diferente do recorte de SP.
BUSCAS_NACIONAIS = [
    ("analista de suporte", None),
    ("suporte de ti", None),
    ("analista de ti", None),
    ("infraestrutura de ti", None),
    ("redes de computacao", None),
    ("analista de infraestrutura", None),
    ("suporte de informatica", None),
    ("seguranca da informacao soc", None),
    ("gerente de ti", None),
    ("help desk", None),
]

# O `innerText` do card traz uma LINHA por bloco: 1ª é o cargo, 2ª a empresa,
# depois data/local/regime/escolaridade. Guardar o texto colapsado com espaços
# misturava a empresa dentro do "cargo" — e era o que fazia "Operador de
# Logística" do GRUPO SUPORTE casar com a área de TI "suporte" (2026-09-27).
JS_CARDS = """() => {
  const out = [], vistos = new Set();
  for (const el of document.querySelectorAll('[data-id][data-href]')) {
    const id = el.getAttribute('data-id');
    const card = el.closest('div[class*="card"], article') || el;
    const bruto = (card.innerText || '');
    const linhas = bruto.split('\\n').map(l => l.trim()).filter(Boolean);
    if (id && linhas.length && !vistos.has(id)) {
      vistos.add(id);
      out.push({
        id,
        titulo: linhas[0],
        empresa: linhas[1] || '',
        texto: bruto.replace(/\\s+/g, ' ').trim().slice(0, 400)
      });
    }
  }
  return out;
}"""


def _vistos():
    try:
        return json.loads(VISTOS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _marcar_vistos(ids):
    atual = _vistos()
    # Janela deslizante: só guardamos o suficiente para não reprocessar. Subiu de
    # 800 para 5000 em 2026-10-03: com as buscas nacionais são ~180 ids novos por
    # rodada, e 800 só cobria ~4 rodadas — as buscas locais (que devolvem sempre
    # os mesmos 20) começavam a ser reprocessadas à toa.
    merged = list(dict.fromkeys(list(ids) + atual))[:5000]
    VISTOS.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")


def _ja_aplicada(ids):
    """IDs cujo resultado já existe — não vamos re-enfileirar o que já foi feito."""
    feitos = set()
    try:
        for linha in S.RESULTADOS.read_text(encoding="utf-8").splitlines():
            if not linha.strip():
                continue
            r = json.loads(linha)
            u = r.get("url") or ""
            for parte in u.split("__"):
                if parte.isdigit():
                    feitos.add(parte)
    except (OSError, ValueError):
        pass
    return ids & feitos


# --- Catho e Vagas.com (2026-10-03) -------------------------------------------------
# Os dois NÃO aceitam a mesma URL do InfoJobs: tentativas por palpite redirecionavam
# para a home. Os padrões abaixo foram lidos das páginas reais (sonda 2026-10-03):
#   Catho     busca  /vagas/<palavra%20com%20%20>   card /vagas/<slug>/<id>
#   Vagas.com busca  /vagas-de-<slug-hifenizado>    card /vagas/v<id>/<slug>
# O "v" antes do número é o que fazia o seletor do Vagas.com casar zero card.
#
# Gupy ficou DE FORA de propósito: não existe busca central. `applicare.gupy.com`
# dá timeout e as vagas ficam em `applicare.gupy.com/<empresa>/` — só há o site
# de cada empresa, o que exigiria uma lista de empresas. Sem isso, coletar Gupy
# é imaginário.
TERMOS_CATHO = [
    "suporte de ti",
    "analista de ti",
    "infraestrutura de ti",
    "analista de suporte",
    "analista de infraestrutura",
    "help desk",
]

TERMOS_VAGAS_COM = [
    "suporte-de-ti",
    "analista-de-ti",
    "infraestrutura-de-ti",
    "analista-de-suporte",
    "analista-de-infraestrutura",
    "redes-de-computacao",
]

JS_CARDS_CATHO = """() => {
  const out = [], vistos = new Set();
  for (const a of document.querySelectorAll('a[href*="/vagas/"]')) {
    const h = a.getAttribute('href') || '';
    const m = h.match(/\\/vagas\\/([^/]+)\\/(\\d{5,})/);
    if (!m || vistos.has(m[2])) continue;
    const box = a.closest('article, li, div[class*=card], div[class*=vaga]') || a;
    const t = (box.innerText || a.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t.length < 12) continue;
    vistos.add(m[2]);
    // titulo = SLUG da URL, nao a 1a linha do card: no Catho o texto comeca com
    // "VAGA PATROCINADA / Publicada em 29/09 / Envio Turbo / Recrutador ativo" e
    // o cargo vem DEPOIS. O slug e o cargo puro — e e o que o classificador usa
    // como areaTexto (o mesmo cuidado que evita o bug do "GRUPO SUPORTE").
    out.push({id: m[2], titulo: decodeURIComponent(m[1]).replace(/-/g, ' '),
              href: h, texto: t.slice(0, 380)});
  }
  return out;
}"""

JS_CARDS_VAGAS_COM = """() => {
  const out = [], vistos = new Set();
  for (const a of document.querySelectorAll('a[href*="/vagas/v"]')) {
    const h = a.getAttribute('href') || '';
    const m = h.match(/\\/vagas\\/v(\\d{5,})\\/([^/?#]+)/);
    if (!m || vistos.has(m[1])) continue;
    const box = a.closest('article, li, div[class*=card], div[class*=vaga]') || a;
    const t = (box.innerText || a.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t.length < 12) continue;
    vistos.add(m[1]);
    out.push({id: m[1], titulo: decodeURIComponent(m[2]).replace(/-/g, ' '),
              href: h, texto: t.slice(0, 380)});
  }
  return out;
}"""


def _coleta_catho(ctx, vistos):
    """Catho: busca por cargo (sem filtro de estado). Sem paginação — a página de
    resultado não expõe `?page=`, então fica na primeira leva."""
    achados = []
    for termo in TERMOS_CATHO:
        url = "https://www.catho.com.br/vagas/" + termo.replace(" ", "%20")
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            cards = page.evaluate(JS_CARDS_CATHO)[:MAX_POR_TERMO]
            novos = 0
            for c in cards:
                if c["id"] in vistos:
                    continue
                vistos.add(c["id"])
                achados.append({
                    # id com prefixo do portal: `coleta.js` monta a chave como
                    # `id:<id>` e os ids NÚMERICOS colidem entre portais (o
                    # 38619055 da Catho pode existir no Vagas.com). Sem o
                    # prefixo, uma das duas seria silenciosamente ignorada.
                    "id": f"catho:{c['id']}",
                    "url": "https://www.catho.com.br" + c["href"].split("?")[0],
                    "titulo": c.get("titulo", ""),
                    "empresa": "",
                    "texto": c["texto"],
                    "portal": "catho",
                    "origem": "coleta direta",
                })
                novos += 1
            print(f"  [catho] {termo}: {len(cards)} cards, {novos} novos", flush=True)
        except Exception as e:
            print(f"  [catho] {termo}: FALHOU {type(e).__name__}: {e}", flush=True)
        finally:
            page.close()
            time.sleep(PAUSA)
    return achados


def _coleta_vagas_com(ctx, vistos):
    """Vagas.com: tem paginação de verdade (`?page=N`), confirmada na página real."""
    achados = []
    for termo in TERMOS_VAGAS_COM:
        for pg in (1, 2):
            url = (f"https://www.vagas.com.br/vagas-de-{termo}"
                   + ("" if pg == 1 else f"?page={pg}"))
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)
                cards = page.evaluate(JS_CARDS_VAGAS_COM)[:MAX_POR_TERMO]
                novos = 0
                for c in cards:
                    if c["id"] in vistos:
                        continue
                    vistos.add(c["id"])
                    achados.append({
                        "id": f"vagas:{c['id']}",
                        "url": "https://www.vagas.com.br" + c["href"].split("?")[0],
                        "titulo": c.get("titulo", ""),
                        "empresa": "",
                        "texto": c["texto"],
                        "portal": "vagas",
                        "origem": "coleta direta",
                    })
                    novos += 1
                print(f"  [vagas.com] {termo} p{pg}: {len(cards)} cards, "
                      f"{novos} novos", flush=True)
            except Exception as e:
                print(f"  [vagas.com] {termo} p{pg}: FALHOU {type(e).__name__}: {e}",
                      flush=True)
            finally:
                page.close()
                time.sleep(PAUSA)
    return achados


def _coleta_infojobs(ctx, vistos):
    achados = []
    # (rótulo, termo, provincia): as locais primeiro, as nacionais depois, para
    # o log mostrar de onde veio cada card.
    alvos = ([("sp" if p else "local", t, p) for t, p in BUSCAS]
             + [("nacional", t, p) for t, p in BUSCAS_NACIONAIS])
    for rotulo, palavra, provincia in alvos:
        url = ("https://www.infojobs.com.br/empregos-trabalho.html"
               f"?palabra={palavra.replace(' ', '+')}")
        if provincia:
            url += f"&provincia={provincia}"
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            cards = page.evaluate(JS_CARDS)[:MAX_POR_TERMO]
            novos = 0
            for c in cards:
                if c["id"] in vistos:
                    continue
                vistos.add(c["id"])
                achados.append({
                    "id": c["id"],
                    # canônico: mesmo formato que `url-vaga.js` gera a partir do `iv`
                    "url": f"https://www.infojobs.com.br/vaga-de-vaga__{c['id']}.aspx",
                    "titulo": c.get("titulo", ""),
                    "empresa": c.get("empresa", ""),
                    "texto": c["texto"],
                    "portal": "infojobs",
                    "origem": "coleta direta",
                })
                novos += 1
            print(f"  [{rotulo:<8}] {palavra}: {len(cards)} cards, {novos} novos", flush=True)
        except Exception as e:
            print(f"  [{rotulo:<8}] {palavra}: FALHOU {type(e).__name__}: {e}", flush=True)
        finally:
            page.close()
            time.sleep(PAUSA)
    return achados


def main():
    if not S.automacao_ligada():
        print("coleta: automação DESLIGADA (botão do widget) — nada coletado")
        return 0

    vistos = set(_vistos())
    # Mesma trava que o watcher do sidecar usa: sem ela, os dois processos sobem
    # o mesmo perfil e um dos launches morre (TargetClosedError).
    with perfil_exclusivo("coleta direta") as ok:
        if not ok:
            print("coleta: perfil ocupado, tentando de novo na próxima rodada")
            return 0
        with sync_playwright() as p:
            ctx = S._lancador(p).launch_persistent_context(
                str(S.PROFILE), headless=S.HEADLESS, env=S._env_browser(),
                viewport={"width": 1400, "height": 900}, locale="pt-BR",
            )
            S._blindar_ctx(ctx)
            try:
                # InfoJobs primeiro: é o que tem filtro de estado e traz o
                # grosso do volume. Catho e Vagas.com depois, na mesma sessão de
                # browser (um launch só, que é a parte cara).
                achados = _coleta_infojobs(ctx, vistos)
                achados += _coleta_catho(ctx, vistos)
                achados += _coleta_vagas_com(ctx, vistos)
            finally:
                ctx.close()

    if not achados:
        print("coleta: nada novo")
        return 0

    ja_feitas = _ja_aplicada({a["id"] for a in achados})
    novas = [a for a in achados if a["id"] not in ja_feitas]
    agora = datetime.now(TZ).isoformat(timespec="seconds")
    with SAIDA.open("a", encoding="utf-8") as fh:
        for a in novas:
            a["coletado_em"] = agora
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")
    _marcar_vistos(a["id"] for a in achados)
    print(json.dumps({"coletadas": len(achados), "ja_aplicadas": len(ja_feitas),
                      "novas": len(novas), "saida": str(SAIDA)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
