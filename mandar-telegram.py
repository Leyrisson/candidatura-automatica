#!/usr/bin/env python3
"""Manda as vagas travadas em `exige_questionario` para o Telegram do dono,
com o link direto, e guarda qual mensagem é de qual vaga.

Guardar o `message_id` é o que fecha o ciclo: quando o dono **responde** a
mensagem, o Telegram entrega o `reply_to_message`, e o bot descobre a vaga
sozinho. Sem esse mapa ele teria que adivinhar, ou obrigar o dono a digitar o
id da vaga a cada resposta.

Respeita o limite de 4096 caracteres do Telegram: corta em blocos em vez de
deixar a API recusar a mensagem inteira.
"""
import json
import re
import os
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
ESTADO = BASE / "telegram-vagas.json"
LIMITE = 3900          # folga do 4096 do Telegram


def _credenciais():
    env = {}
    for l in ENV.read_text(encoding="utf-8").splitlines():
        l = l.strip()
        if l and "=" in l and not l.startswith("#"):
            k, _, v = l.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    return env


def estado():
    """{'enviadas': [...], 'mensagens': {message_id: url}} — tolera estado velho."""
    try:
        d = json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if isinstance(d, list):            # formato antigo: era so lista de urls
        d = {"enviadas": d, "mensagens": {}}
    d.setdefault("enviadas", [])
    d.setdefault("mensagens", {})
    return d


def grava_estado(d):
    tmp = ESTADO.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ESTADO)                # substitui de uma vez: nunca meio arquivo


def vagas(reenviar=False):
    """Vagas presas em questionario que ainda nao foram avisadas no Telegram."""
    feitas = set() if reenviar else set(estado()["enviadas"])
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


def perguntas_de(url):
    """Perguntas de uma vaga, lidas da fonte de verdade (o jsonl da coleta)."""
    for l in RES.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        try:
            v = json.loads(l)
        except ValueError:
            continue
        if (v.get("url") or "").split("?")[0] == url:
            return [q.strip() for q in (v.get("perguntas") or []) if q.strip()]
    return []


def limpa(texto):
    """Tira os caracteres que o Markdown do Telegram usa como marcação.

    As perguntas da Catho vêm com `*` no fim, marcando campo obrigatório no
    portal. Numa mensagem com 9 vagas esses `*` se casavam por acidente entre
    uma vaga e outra e o Telegram aceitava; com UMA mensagem por vaga o `*`
    fica sem par e a API devolve 400 "can't parse entities". Tirar a marcação
    na origem evita depender desse acidente."""
    return re.sub(r"[*_`\[]", "", str(texto or "")).strip()


def bloco(v):
    t = limpa((v.get("titulo") or "").replace("Vaga de Emprego de ", ""))
    t = t.rstrip(" ,/")[:64]
    linhas = [f"*{t}*"] if t else []
    for p in (v.get("perguntas") or [])[:5]:
        p = limpa(p)
        if p:
            linhas.append(f"  - {p}")
    linhas.append(f"  {v.get('url')}")
    return "\n".join(linhas)


def manda(token, chat, texto):
    data = urllib.parse.urlencode({"chat_id": chat, "text": texto,
                                   "parse_mode": "Markdown",
                                   "disable_web_page_preview": "false"}
                                  ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def main():
    env = _credenciais()
    token = env.get("TELEGRAM_BOT_TOKEN") or env.get("BOT_TOKEN")
    chat = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("token/chat ausente em", ENV)
        return 1

    d = estado()

    if "--listar" in sys.argv:
        for mid, url in sorted(d["mensagens"].items(), key=lambda x: int(x[0])):
            print(f"msg {mid}: {url}")
            for q in perguntas_de(url):
                print(f"    - {q}")
        return 0

    vs = vagas(reenviar="--reenviar" in sys.argv)
    if not vs:
        print("nenhuma vaga travada nova para avisar")
        return 0

    cab = ("*Vaga travada esperando sua resposta*\n"
           "**Responda esta mensagem** que eu registro e te mostro antes de "
           "enviar.\n\n")

    # Uma mensagem POR VAGA, mesmo cabendo todas numa so. Juntas num unico
    # texto, as 9 vagas dividiriam um unico message_id: responder a mensagem nao
    # diria QUAL vaga, e o dono teria que repetir o id a cada resposta. Uma por
    # vaga deixa o "responder" inequivoco.
    #
    # Texto e message_id sao montados na MESMA passada, senao o indice pode
    # andar e o id ficar associado a vaga errada.
    enviadas = []
    for i, v in enumerate(vs, 1):
        texto = cab + bloco(v)
        # rede de seguranca: uma vaga com 20 perguntas nao pode estourar 4096
        while len(texto) > LIMITE and len(v.get("perguntas") or []) > 1:
            v["perguntas"] = v["perguntas"][:-1]
            texto = cab + bloco(v)
        try:
            r = manda(token, chat, texto)
            if not r.get("ok"):
                print(f"vaga {i}/{len(vs)}: {r.get('description')}")
                break
            mid = r.get("result", {}).get("message_id")
            enviadas.append(((v.get("url") or "").split("?")[0], mid))
            print(f"vaga {i}/{len(vs)}: ok (msg {mid}) -> "
                  f"{(v.get('url') or '').split('/')[-1]}")
        except Exception as e:
            print(f"vaga {i}/{len(vs)}: FALHOU {e}")
            break
        time.sleep(0.6)

    if not enviadas:
        print("nada enviado; as voltam na proxima rodada")
        return 1
    for url, mid in enviadas:
        if mid:
            d["mensagens"][str(mid)] = url
    d["enviadas"] = sorted(set(d["enviadas"])
                           | {u for u, _ in enviadas})
    grava_estado(d)
    print(f"estado gravado: {len(d['enviadas'])} vagas, "
          f"{len(d['mensagens'])} mensagens mapeadas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
