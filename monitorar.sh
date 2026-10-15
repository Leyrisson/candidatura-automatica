#!/usr/bin/env bash
# Monitora candidaturas e regenera o relatório.
#
#   monitorar.py -> reabre as vagas já candidatadas e avisa se a empresa
#                   respondeu ou se a vaga encerrou (monitoramento.json)
#   relatorio.py -> recontagem + o que ainda depende do dono
#
# A trava de perfil fica no monitorar.py: se a coleta de 30 min estiver no
# browser, ele espera em vez de derrubar a sessão um do outro.
set -uo pipefail

BASE="$HOME/n8n-docker/workflows/host/candidatura"
VENV="$BASE/browser/.venv"
LOG="$BASE/monitor.log"

cd "$BASE" || exit 1

{
  echo "=== $(date '+%F %T') monitor ==="
  VAGAS_HEADLESS=1 "$VENV/bin/python" "$BASE/monitorar.py" --limite 12
  rc_mon=$?
  "$VENV/bin/python" "$BASE/relatorio.py"
  rc_rel=$?
  # Batimento cardíaco do monitor (ver `saude.py` e o comentário em `coleta.sh`).
  if [ "$rc_mon" -eq 0 ] && [ "$rc_rel" -eq 0 ]; then
    "$VENV/bin/python" "$BASE/saude.py" ok monitor
  else
    "$VENV/bin/python" "$BASE/saude.py" falha monitor "monitorar.py=$rc_mon relatorio.py=$rc_rel"
  fi
} >> "$LOG" 2>&1

tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
