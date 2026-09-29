#!/usr/bin/env python3
"""Sonda: o que os cards de busca do InfoJobs realmente entregam.

Não é o coletor — é a inspeção que evita escrever no escuro. Roda 1x, imprime o
que achou e morre.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sidecar as S  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

URL = ("https://www.infojobs.com.br/empregos-trabalho.html"
       "?palabra=analista+de+suporte&provincia=8")

JS_CARDS = """() => {
  const out = [];
  const vistos = new Set();
  for (const el of document.querySelectorAll('[data-id][data-href]')) {
    const href = el.getAttribute('data-href') || '';
    const card = el.closest('div[class*="card"], article') || el;
    const txt = (card.innerText || '').replace(/\\s+/g, ' ').trim();
    if (href && !vistos.has(href)) {
      vistos.add(href);
      out.push({href, id: el.getAttribute('data-id'), texto: txt.slice(0, 160)});
    }
  }
  return out;
}"""


def main():
    with _lock():
        with sync_playwright() as p:
            ctx = S._lancador(p).launch_persistent_context(
                str(S.PROFILE), headless=S.HEADLESS, env=S._env_browser(),
                viewport={"width": 1400, "height": 900}, locale="pt-BR",
            )
            S._blindar_ctx(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)
            print("url final:", page.url)
            print("title:", page.title()[:90])
            cards = page.evaluate(JS_CARDS)
            print("cards:", len(cards))
            for c in cards[:4]:
                print(" -", c["id"], c["href"][:62], "|", c["texto"][:70])
            ctx.close()


def _lock():
    return S._BROWSER_LOCK


if __name__ == "__main__":
    main()
