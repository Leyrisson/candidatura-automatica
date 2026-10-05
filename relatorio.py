#!/usr/bin/env python3
"""Relatório de candidaturas: quantas saíram, onde, e o que ainda depende do dono.

Lê o resultado-vagas.jsonl (que é a fonte mais rica: tem portal, etapa, enviado,
perguntas e linkCandidatura) e escreve um markdown. Sem browser, sem rede — só
conta o que já aconteceu. Serve tanto para a análise pedida quanto para o
monitorio Diário.
"""
import collections
import json
import os
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
RES = BASE / "resultado-vagas.jsonl"
SAIDA = BASE / "relatorio-candidaturas.md"
ESTADO = BASE / "estado-candidaturas.json"

TZ = datetime.now().astimezone().tzinfo

# Etapa que significa "a candidatura saiu daqui mesmo". `externa` NAO conta: o
# Vagas.com manda pro chat de triagem por IA e quem candidatou é o dono.
ETAPAS_ENVIADAS = {"candidatado"}
# Etapas que parado esperando alguma coisa do dono.
ETAPAS_PENDENTES = {"exige_questionario", "externa", "pronta_para_candidatar",
                    "sem_confirmação"}


def carregar():
    if not RES.exists():
        return []
    out = []
    for linha in RES.read_text(encoding="utf-8").splitlines():
        if not linha.strip():
            continue
        try:
            out.append(json.loads(linha))
        except ValueError:
            pass
    return out


def titulo_curto(v):
    t = (v.get("titulo") or "").strip()
    if not t:
        t = (v.get("subject") or v.get("url") or "").rstrip("/").split("/")[-1]
    return t[:70]


def main():
    r = carregar()
    agora = datetime.now(TZ)
    enviados = [v for v in r if v.get("etapa") in ETAPAS_ENVIADAS and v.get("enviado")]
    pendentes = [v for v in r if v.get("etapa") in ETAPAS_PENDENTES]
    barradas = [v for v in r if v.get("etapa") in ("regime_incompativel", "exige_superior",
                                                   "exige_certificacao", "encerrada")]

    # Uma vaga por URL: o mesmo emprego pode ter mais de um registro.
    def por_url(lst):
        d = {}
        for v in lst:
            d[(v.get("url") or "").split("?")[0]] = v
        return list(d.values())

    enviados, pendentes, barradas = por_url(enviados), por_url(pendentes), por_url(barradas)

    hoje = [v for v in enviados if str(v.get("ts", "")).startswith(agora.strftime("%Y-%m-%d"))]
    por_portal = collections.Counter(v.get("portal") or "?" for v in enviados)
    por_etapa = collections.Counter(v.get("etapa") for v in r)

    L = []
    L.append(f"# Candidaturas — {agora.strftime('%d/%m/%Y %H:%M')}\n")
    L.append(f"**{len(enviados)} candidaturas reais enviadas** "
             f"({len(hoje)} hoje)\n")
    if por_portal:
        L.append("| Portal | Enviadas |")
        L.append("|---|---|")
        for p, n in por_portal.most_common():
            L.append(f"| {p} | {n} |")
        L.append("")

    L.append("## Fila parada esperando você\n")
    if not pendentes:
        L.append("Nada. ✅\n")
    else:
        grupos = collections.defaultdict(list)
        for v in pendentes:
            grupos[v.get("etapa")].append(v)
        for etapa in sorted(grupos, key=lambda e: -len(grupos[e])):
            L.append(f"### {etapa} ({len(grupos[etapa])})\n")
            for v in grupos[etapa]:
                L.append(f"- **{titulo_curto(v)}** — {v.get('portal') or '?'}")
                # `detalhe` costuma ser a lista de perguntas de novo
                # ("perguntas=..."), e as perguntas já saem abaixo.
                det = (v.get("detalhe") or "")
                if det and not det.startswith("perguntas="):
                    L.append(f"  - {det}")
                for p in (v.get("perguntas") or [])[:4]:
                    L.append(f"  - ❓ {p}")
                if v.get("linkCandidatura"):
                    L.append(f"  - link: {v['linkCandidatura']}")
                elif v.get("url"):
                    L.append(f"  - {v['url']}")
            L.append("")

    L.append("## Reprovadas por critério seu\n")
    mot = collections.Counter(v.get("etapa") for v in barradas)
    if mot:
        L.append("| Motivo | Vagas |")
        L.append("|---|---|")
        for m, n in mot.most_common():
            L.append(f"| {m} | {n} |")
    else:
        L.append("Nenhuma.\n")

    L.append("\n---\nTodas as etapas no histórico:\n")
    L.append("| Etapa | Ocorrências |")
    L.append("|---|---|")
    for e, n in por_etapa.most_common():
        L.append(f"| {e} | {n} |")

    md = "\n".join(L) + "\n"
    SAIDA.write_text(md, encoding="utf-8")

    ESTADO.write_text(json.dumps({
        "gerado_em": agora.isoformat(timespec="seconds"),
        "enviadas": len(enviados),
        "hoje": len(hoje),
        "pendentes": len(pendentes),
        "por_portal": dict(por_portal),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(json.dumps({"enviadas": len(enviados), "hoje": len(hoje),
                      "pendentes": len(pendentes),
                      "por_portal": dict(por_portal)}, ensure_ascii=False))
    print(f"-> {SAIDA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
