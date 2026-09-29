#!/usr/bin/env bash
# Coleta direta: raspa os portais, classifica e enfileira.
#
#   browser/coleta.py  -> pega os cards das páginas de busca (InfoJobs)
#   coleta.js          -> classifica e escreve na fila
#
# O `alternar.py` é a bandeira: com a automação desligada (botão do widget) os
# dois Scripts saem sem fazer nada, então este timer pode continuar rodando.
set -uo pipefail

BASE="$HOME/n8n-docker/workflows/host/candidatura"
VENV="$BASE/browser/.venv"
LOG="$BASE/coleta.log"

cd "$BASE" || exit 1

{
  echo "=== $(date '+%F %T') coleta direta ==="
  # Sem LLM no lote: a heurística é a primária e o desempate por LLM (120s de
  # timeout por vaga) transformaria 80 vagas em horas de espera.
  VAGAS_HEADLESS=1 "$VENV/bin/python" "$BASE/browser/coleta.py"
  VAGAS_SEM_LLM=1 node "$BASE/coleta.js"
} >> "$LOG" 2>&1

# O log vira Infinite-scroll: corta mantendo as últimas 400 linhas.
tail -n 400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
