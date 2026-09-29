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
MAX_POR_TERMO = 12          # nem raspa a página inteira: 12 é o suficiente
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
    # Janela deslizante: só guardamos o suficiente para não reprocessar.
    merged = list(dict.fromkeys(list(ids) + atual))[:800]
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


def _coleta_infojobs(ctx, vistos):
    achados = []
    for palavra, provincia in BUSCAS:
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
            print(f"  {palavra}: {len(cards)} cards, {novos} novos", flush=True)
        except Exception as e:
            print(f"  {palavra}: FALHOU {type(e).__name__}: {e}", flush=True)
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
                achados = _coleta_infojobs(ctx, vistos)
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
