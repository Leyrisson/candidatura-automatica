#!/usr/bin/env python3
"""Liga/desliga a automação de candidaturas.

É o que o botão do widget (Omarchy) chama. Faz as TRÊS coisas que
"desligar a automação" tem que significar, senão o botão vira enfeite:

  1. grava `estado.json` (a-bandeira que o pipeline e o sidecar consultam);
  2. para/sobe o serviço `vagas-sidecar` (o browser que de fato aplica) — o
     corte de verdade, porque nenhum processo fica segurando fila;
  3. marca a origem, para o rodapé do widget dizer quem desligou.

Uso:
  ./alternar.py            alterna
  ./alternar.py on|off     liga / desliga
  ./alternar.py status     imprime o estado, sai 0 se ligado, 1 se desligado
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
ESTADO = Path(
    os.environ.get(
        "VAGAS_ESTADO", Path.home() / "n8n-docker/workflows/host/candidatura/estado.json"
    )
)
SERVICO = os.environ.get("VAGAS_SERVICO", "vagas-sidecar.service")


def ler():
    try:
        d = json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Sem arquivo (ou corrompido) a automação fica DESLIGADA: na dúvida
        # não Candidatar para vaga nenhuma.
        return {"ativo": False, "atualizado_em": "", "motivo": "estado ilegível", "origem": "?"}
    return d


def gravar(ativo, motivo, origem="widget"):
    d = ler()
    d.update(
        ativo=bool(ativo),
        motivo=motivo,
        origem=origem,
        atualizado_em=datetime.now(TZ).isoformat(timespec="seconds"),
    )
    tmp = ESTADO.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(ESTADO)  # troca atômica: o sidecar lê sem ver arquivo pela metade
    return d


def servico(acao):
    """start/stop do sidecar. Não levanta exceção: o estado já foi gravado."""
    if acao not in ("start", "stop"):
        return ""
    r = subprocess.run(
        ["systemctl", "--user", acao, SERVICO],
        capture_output=True, text=True, timeout=60,
    )
    return r.stderr.strip() if r.returncode else ""


def aplica(ativo, origem="widget"):
    d = gravar(ativo, "ligado" if ativo else "desligado", origem)
    erro = servico("start" if ativo else "stop")
    d["servico_erro"] = erro
    return d


def main():
    args = sys.argv[1:]
    if args and args[0] == "status":
        d = ler()
        print(json.dumps(d, ensure_ascii=False))
        return 0 if d.get("ativo") else 1
    alvo = None
    if args and args[0] in ("on", "off", "ligar", "desligar"):
        alvo = args[0] in ("on", "ligar")
    if alvo is None:
        alvo = not ler().get("ativo")
    origem = "terminal" if args and args[0] in ("on", "off", "ligar", "desligar") else "widget"
    d = aplica(alvo, origem)
    if d.get("servico_erro"):
        print(f"atencao: systemctl falhou: {d['servico_erro']}", file=sys.stderr)
    print(json.dumps({"ativo": d["ativo"], "motivo": d["motivo"],
                      "atualizado_em": d["atualizado_em"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
