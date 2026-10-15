#!/usr/bin/env python3
"""Batimento cardíaco da automação de vagas — para o sistema não morrer em silêncio.

Motivação (2026-10-14): o driver do Playwright subiu para 1.63.0, o navegador
correspondente não estava instalado e **coleta e monitor morreram por 8 dias**
sem nenhum alerta. A única pista era `hoje: 0` no relatório — que ninguém olha
quando o widget parece "verde".

Como funciona:
  - `saude.py ok <fonte>`    -> grava o instante do último SUCESSO (fonte: coleta|monitor).
                                Chamado pelos wrappers `coleta.sh`/`monitorar.sh` só
                                quando os scripts saem com código 0.
  - `saude.py falha <fonte> <detalhe>` -> registra a tentativa e o erro, sem apagar
                                o último sucesso (o que importa é há quanto tempo
                                não há sucesso).
  - `saude.py checar`        -> o vigia (timer próprio): se a automação está LIGADA
                                e a coleta não conclui com sucesso há mais de LIMITE_H
                                horas, manda UM alerta no Telegram (não repete a cada
                                rodada enquanto o problema persistir).

Sem dependência além da biblioteca padrão. Lê o token do Telegram do mesmo
`credenciais.env` do telebot, como o `mandar-telegram.py` — não duplica segredo.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
ESTADO_AUTO = Path(os.environ.get("VAGAS_ESTADO", BASE / "estado.json"))
SAUDE = BASE / "saude.json"
ENV = Path(os.environ.get(
    "TELEBOT_ENV",
    Path.home() / ".config/omarchy-voice/plugins/telebot" / "credenciais.env"))

# A coleta roda a cada 30 min; 2h = 4 rodadas perdidas. Abaixo disso seria alarme
# falso a cada lentidão de portal; acima, demora demais para o dono perceber.
LIMITE_H = float(os.environ.get("VAGAS_SAUDE_LIMITE_H", "2"))
# Fonte observada pelo vigia: a coleta é a mais frequente (30 min) e a que
# primeiro denuncia o problema (foi a que quebrou em 06/10).
FONTE_VIGIADA = os.environ.get("VAGAS_SAUDE_FONTE", "coleta")


def _agora():
    return datetime.now().astimezone()


def _auto_ligada():
    try:
        return json.loads(ESTADO_AUTO.read_text(encoding="utf-8")).get("ativo") is True
    except (OSError, ValueError):
        return False


def _mtime(p):
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).astimezone()
    except OSError:
        return None


def _le():
    try:
        d = json.loads(SAUDE.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    d.setdefault("fontes", {})
    d.setdefault("ultimo_alerta", None)
    return d


def _grava(d):
    tmp = SAUDE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SAUDE)          # troca de uma vez: nunca deixa arquivo pela metade


def _credenciais():
    env = {}
    for l in ENV.read_text(encoding="utf-8").splitlines():
        l = l.strip()
        if l and "=" in l and not l.startswith("#"):
            k, _, v = l.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    return env


def _manda(texto):
    env = _credenciais()
    token = env.get("TELEGRAM_BOT_TOKEN") or env.get("BOT_TOKEN")
    chat = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(f"saude: token/chat ausente em {ENV}")
        return False
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": texto, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception as e:                      # rede/token/parse: nunca derruba o vigia
        print(f"saude: erro ao enviar alerta: {type(e).__name__}: {e}")
        return False


def registra_ok(fonte):
    d = _le()
    st = d["fontes"].setdefault(fonte, {})
    agora = _agora().isoformat(timespec="seconds")
    st["ultimo_sucesso"] = agora
    st["ultima_tentativa"] = agora
    st["ultimo_erro"] = None
    _grava(d)
    print(f"saude: heartbeat ok ({fonte})")
    return 0


def registra_falha(fonte, detalhe=""):
    d = _le()
    st = d["fontes"].setdefault(fonte, {})
    st["ultima_tentativa"] = _agora().isoformat(timespec="seconds")
    st["ultimo_erro"] = str(detalhe or "")[:400]
    _grava(d)
    print(f"saude: falha registrada ({fonte}): {st['ultimo_erro']}")
    return 0


def checar():
    if not _auto_ligada():
        print("saude: automação DESLIGADA (bandeira) — sem alerta")
        return 0
    agora = _agora()

    # Carência: se a bandeira acabou de ser ligada, a primeira coleta boa ainda
    # não teve tempo de rodar (roda a cada 30 min). Sem isso, o vigia dispara um
    # alarme falso logo depois de o dono ligar o botão.
    m = _mtime(ESTADO_AUTO)
    if m and (agora - m).total_seconds() < LIMITE_H * 3600:
        print("saude: bandeira alterada há pouco — em carência, sem alerta")
        return 0

    d = _le()
    st = d["fontes"].get(FONTE_VIGIADA, {})
    us = st.get("ultimo_sucesso")
    if not us:
        print(f"saude: sem heartbeat de '{FONTE_VIGIADA}' ainda (primeira rodada)")
        return 0

    idade_h = (agora - datetime.fromisoformat(us)).total_seconds() / 3600.0
    if idade_h < LIMITE_H:
        print(f"saude: ok — '{FONTE_VIGIADA}' com sucesso há {idade_h:.1f}h")
        return 0

    # Está parado. Não repetir o alerta a cada rodada enquanto o dono não resolver.
    ult = d.get("ultimo_alerta")
    if ult and (agora - datetime.fromisoformat(ult)).total_seconds() < LIMITE_H * 3600:
        print(f"saude: '{FONTE_VIGIADA}' parada há {idade_h:.1f}h — já alertado")
        return 0

    erro = st.get("ultimo_erro")
    texto = ["⚠️ Automação de vagas PARADA",
             f"A {FONTE_VIGIADA} não conclui com sucesso há {idade_h:.1f}h "
             f"(limite {LIMITE_H:g}h)."]
    if erro:
        texto.append(f"Último erro: {erro}")
    texto.append("Veja: systemctl --user status vagas-coleta / coleta.log")
    if _manda("\n".join(texto)):
        d["ultimo_alerta"] = agora.isoformat(timespec="seconds")
        _grava(d)
        print(f"saude: alerta enviado (parada há {idade_h:.1f}h)")
        return 0
    print("saude: falha ao enviar alerta no Telegram")
    return 1


def main():
    if len(sys.argv) < 2:
        print("uso: saude.py ok|falha|checar [fonte] [detalhe]")
        return 2
    cmd = sys.argv[1]
    if cmd == "ok":
        return registra_ok(sys.argv[2] if len(sys.argv) > 2 else FONTE_VIGIADA)
    if cmd == "falha":
        return registra_falha(sys.argv[2] if len(sys.argv) > 2 else FONTE_VIGIADA,
                              sys.argv[3] if len(sys.argv) > 3 else "")
    if cmd == "checar":
        return checar()
    print(f"saude: comando desconhecido: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
