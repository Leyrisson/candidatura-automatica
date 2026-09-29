#!/usr/bin/env python3
"""Reescreve no vagas.db as candidaturas que existiram antes de o sidecar
gravar etapa `candidatado` no banco (rodadas manuais enfileiradas na mão).

Fontes:
  resultado-vagas.jsonl        — resultados já produzidos pelo sidecar
  historico-candidaturas.json   — candidaturas anteriores, da nota do projeto

Idempotente: pula o que já está em `vagas` (por message_id). Não toca no
resultado-vagas.jsonl — apagar linha de lá faz o watcher reprocessar a vaga e
candidatar de novo.

Uso:  python3 backfill-resultados.py [--dry-run]
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
VAGAS_DB = Path(os.environ.get("VAGAS_DB", BASE.parent / "vagas.db"))
RESULTADOS = BASE / "resultado-vagas.jsonl"
HISTORICO = BASE / "historico-candidaturas.json"
TZ = ZoneInfo("America/Sao_Paulo")
INTERESSANTES = ("candidatado", "exige_questionario", "ja_candidatado",
                 "externa", "encerrada", "sem_botao", "pagina_lida")


def _utc(ts):
    """'2026-09-24T22:00:00-03:00' (ou mtime) -> literal SQL UTC
    'YYYY-MM-DD HH:MM:SS', que é o formato de `etapas.criado_em`
    (datetime('now') grava em UTC)."""
    if not ts:
        return None
    try:
        return "'" + datetime.fromisoformat(ts).astimezone(
            ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S") + "'"
    except Exception:
        return None


def _do_jsonl():
    if not RESULTADOS.exists():
        return []
    mtime = datetime.fromtimestamp(RESULTADOS.stat().st_mtime, TZ).isoformat(timespec="seconds")
    itens = []
    for linha in RESULTADOS.read_text(encoding="utf-8").splitlines():
        if not linha.strip():
            continue
        try:
            r = json.loads(linha)
        except Exception:
            continue
        if r.get("erro") or r.get("etapa") not in INTERESSANTES:
            continue
        itens.append({
            "messageId": r.get("messageId") or "",
            "url": r.get("url") or "",
            "titulo": r.get("titulo") or "",
            "etapa": r.get("etapa"),
            "detalhe": r.get("detalhe") or "",
            "ts": r.get("ts") or mtime,
        })
    return itens


def _do_historico():
    if not HISTORICO.exists():
        return []
    return json.loads(HISTORICO.read_text(encoding="utf-8")).get("candidaturas", [])


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(VAGAS_DB, timeout=15)
    con.execute("PRAGMA busy_timeout=10000")
    conhecidos = {r[0] for r in con.execute("SELECT message_id FROM vagas")}
    inseridos, pulados = 0, 0
    for item in _do_jsonl() + _do_historico():
        msg = item.get("messageId") or ""
        if not msg:
            continue
        if msg in conhecidos:
            pulados += 1
            continue
        ts_sql = _utc(item.get("ts")) or "datetime('now')"
        etapa = item.get("etapa")
        url = item.get("url") or ""
        titulo = (item.get("titulo") or "")[:300]
        detalhe = (item.get("detalhe") or etapa)[:400]
        if dry:
            print(f"[dry] {msg:26s} {etapa:18s} {ts_sql}  {titulo[:50]}")
            conhecidos.add(msg)
            inseridos += 1
            continue
        con.execute(
            f"INSERT OR IGNORE INTO vagas (message_id, subject, link, etapa, origem, "
            f"criado_em, atualizado_em) VALUES (?,?,?,?,?,{ts_sql},{ts_sql})",
            (msg, titulo, url, etapa, url))
        con.execute(
            f"INSERT INTO etapas (vaga_id, etapa, detalhe, criado_em) "
            f"SELECT id, ?, ?, {ts_sql} FROM vagas WHERE message_id=?",
            (etapa, detalhe, msg))
        conhecidos.add(msg)
        inseridos += 1
    if not dry:
        con.commit()
    con.close()
    print(f"inseridos={inseridos} ja_existiam={pulados}" + (" (dry-run, nada gravado)" if dry else ""))


if __name__ == "__main__":
    main()
