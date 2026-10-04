#!/usr/bin/env python3
"""Manda as vagas travadas em `exige_questionario` para o Telegram do dono,
com o link direto da vaga para ele responder la mesmo.

Respeita o limite de 4096 caracteres do Telegram: corta em blocos em vez de
deixar a API recusar a mensagem inteira.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
RES = BASE / "resultado-vagas.jsonl"
# Onde o token do Telegram mora. Aponta por padrao para o telebot, que
# ja tem esse arquivo; TELEBOT_ENV sobrescreve para outro lugar.
ENV = Path(os.environ.get("TELEBOT_ENV",
                     Path.home()
                     / ".config/omarchy-voice/plugins/telebot"
                     / "credenciais.env"))
LIMITE = 3900          # folga do 4096 do Telegram
# Quais vagas ja foram mandadas. Sem isso, rodar de novo (ou o timer disparar)
# manda as mesmas 9 de novo e o dono recebe copia — aconteceu em 04/10.
ENVIADO = BASE / "telegram-enviado.json"


def _credenciais():
    env = {}
    for l in ENV.read_text(encoding="utf-8").splitlines():
        l = l.strip()
        if l and "=" in l and not l.startswith("#"):
            k, _, v = l.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    return env


def ja_enviadas():
    try:
        return set(json.loads(ENVIADO.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def marca_enviadas(urls):
    """Escreve o estado depois do envio, e nao antes: se a API falhar no meio,
    a vaga volta para a proxima rodada em vez de ficar perdida para sempre."""
    try:
        ENVIADO.write_text(json.dumps(sorted(urls), ensure_ascii=False,
                                         indent=1), encoding="utf-8")
    except OSError as e:
        print(f"aviso: nao consegui gravar {ENVIADO}: {e}")


def vagas(reenviar=False):
    """Vagas presas em questionario que ainda nao foram avisadas no Telegram."""
    feitas = set() if reenviar else ja_enviadas()
    fora, vistos = [], set()
    for l in RES.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        try:
            v = json.loads(l)
        except ValueError:
            continue
        if v.get("etapa") != "exige_questionario":
            continue
        u = (v.get("url") or "").split("?")[0]
        if not u or u in vistos or u in feitas:
            continue
        vistos.add(u)
        fora.append(v)
    return fora


def bloco(v):
    t = (v.get("titulo") or "").replace("Vaga de Emprego de ", "").strip()
    t = t.rstrip(" ,/")[:64]
    linhas = [f"*{t}*"]
    for p in (v.get("perguntas") or [])[:5]:
        linhas.append(f"  - {p.strip()}")
    linhas.append(f"  {v.get('url')}")
    return "\n".join(linhas)


def main():
    env = _credenciais()
    token = env.get("TELEGRAM_BOT_TOKEN") or env.get("BOT_TOKEN")
    chat = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("token/chat ausente em", ENV)
        return 1

    vs = vagas(reenviar="--reenviar" in sys.argv)
    if not vs:
        print("nenhuma vaga travada nova para avisar")
        return 0

    cab = ("*Vagas travadas esperando sua resposta* "
           f"({len(vs)})\nCada link abre a vaga ja no formulario. "
           "Responda aqui no Telegram que eu gravo a resposta.\n")
    partes, atual = [], cab
    for v in vs:
        b = bloco(v)
        if len(atual) + len(b) + 2 > LIMITE and atual != cab:
            partes.append(atual)
            atual = b
        else:
            atual += "\n\n" + b
    if atual.strip():
        partes.append(atual)

    # Só marca depois que TODAS as partes saíram. Marcando no meio, uma falha
    # na 2a parte deixaria a 3a perdida para sempre; marcando no fim com o
    # break, as ja-enviadas simplesmente voltam na proxima rodada.
    completo = True
    for i, p in enumerate(partes, 1):
        data = urllib.parse.urlencode({"chat_id": chat, "text": p,
                                       "parse_mode": "Markdown",
                                       "disable_web_page_preview": "false"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read())
            print(f"parte {i}/{len(partes)}: {'ok' if d.get('ok') else d.get('description')}")
        except Exception as e:
            print(f"parte {i}: FALHOU {e}")
            completo = False
            break
        time.sleep(1)

    if completo:
        marca_enviadas(ja_enviadas()
                       | {v["url"].split("?")[0] for v in vs})
    else:
        print("nada marcado; as vagas voltam na proxima rodada")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
