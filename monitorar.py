#!/usr/bin/env python3
"""Monitora as vagas em que já se candidatou, procurando MUDANÇA de estado.

O que ele olha, por vaga já resolvida:
  - a vaga ainda existe ou foi encerrada;
  - apareceu alguma coisa que é resposta da empresa (processo seletivo,
    entrevista, contato, currículo em análise);
  - para a Catho, o "Meus currículos" é a fonte real de status — mas isso exige
    uma tela autenticada separada, então aqui fica a leitura da própria vaga.

Guarda o estado em `monitoramento.json` e só avisa quando algo MUDA, porque uma
notificação a cada rodada vira ruído e o dono deixa de olhar.

Uso:
    monitorar.py            # até --limite vagas (default 12), as mais antigas primeiro
    monitorar.py --limite 40
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("VAGAS_HEADLESS", "1")
BASE = Path(__file__).resolve().parent
BROWSER = BASE / "browser"
sys.path.insert(0, str(BROWSER))

RES = BASE / "resultado-vagas.jsonl"
ESTADO = BASE / "monitoramento.json"
SAIDA = BASE / "monitoramento-relatorio.md"

TZ = datetime.now().astimezone().tzinfo

# Sinais de que a empresa respondeu. Britânico/devagar: qualquer um destes é
# motivo para avisar, então prefiro sinal forte a sinal fraco.
RE_EMPRESA = re.compile(
    r"em an[aá]lise|processo seletivo|entrevista|entrou em contato|"
    r"seu currículo (foi )?recebido|currículo em an[aá]lise|"
    r"foi enviado para|retorno|aguardando", re.I)
RE_FECHADA = re.compile(
    r"vaga encerrada|encerrada|n[aã]o est[aá] mais dispon[ií]vel|"
    r"esta vaga n[aã]o existe|candidatura encerrada|posiç[aã]o preenchida", re.I)
RE_ABERTA = re.compile(r"quero me candidatar", re.I)


def carregar_res():
    if not RES.exists():
        return []
    out = []
    for l in RES.read_text(encoding="utf-8").splitlines():
        if l.strip():
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
    return out


def normaliza(u):
    return (u or "").split("#")[0].split("?")[0]


def estado_de(texto):
    """Traduz o texto da página num status curto, ou None se nada direrente."""
    if RE_FECHADA.search(texto):
        return "encerrada"
    if RE_EMPRESA.search(texto):
        return "em_analise"
    if RE_ABERTA.search(texto):
        return "aberta"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=12)
    args = ap.parse_args()

    import sidecar as S
    from lock_perfil import perfil_exclusivo
    from playwright.sync_api import sync_playwright

    # Só as já candidatadas (ou em aberto esperando), uma por URL.
    vistos, alvos = set(), []
    for v in carregar_res():
        u = normaliza(v.get("url"))
        if not u or u in vistos:
            continue
        vistos.add(u)
        if v.get("etapa") in ("candidatado", "exige_questionario",
                              "pronta_para_candidatar"):
            alvos.append(v)
    # mais antigas primeiro: são as que têm mais chance de já ter resposta
    alvos.sort(key=lambda v: str(v.get("ts") or ""))
    alvos = alvos[:args.limite]

    if not alvos:
        print(json.dumps({"monitoradas": 0}, ensure_ascii=False))
        return 0

    estado = {}
    if ESTADO.exists():
        try:
            estado = json.loads(ESTADO.read_text(encoding="utf-8"))
        except ValueError:
            estado = {}

    mudancas, checadas = [], 0
    with perfil_exclusivo("monitor") as ok:
        if not ok:
            print("perfil ocupado; monitoracao adiada")
            return 0
        with sync_playwright() as p:
            ctx = S._lancador(p).launch_persistent_context(
                str(S.PROFILE), headless=S.HEADLESS, env=S._env_browser(),
                viewport={"width": 1400, "height": 900}, locale="pt-BR")
            S._blindar_ctx(ctx)
            try:
                for v in alvos:
                    page = ctx.new_page()
                    u = normaliza(v.get("url"))
                    try:
                        page.goto(u, wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(2500)
                        st = estado_de(S._texto_visivel(page))
                        checadas += 1
                        antes = estado.get(u, {}).get("status")
                        registro = {"status": st, "visto_em":
                                    datetime.now(TZ).isoformat(timespec="seconds")}
                        estado[u] = registro
                        # Primeira leitura NÃO é mudança: é só o baseline. Sem
                        # isso, a primeira rodada "acha" 12 mudanças e o dono
                        # passa a ignorar o monitor inteiro.
                        if st and antes and st != antes:
                            mudancas.append({"url": u, "titulo": (v.get("titulo") or "")[:70],
                                             "de": antes, "para": st})
                    except Exception as e:
                        print(f"  falha em {u[-30:]}: {type(e).__name__}", flush=True)
                    finally:
                        page.close()
            finally:
                ctx.close()

    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=1),
                      encoding="utf-8")

    L = [f"# Monitoramento — {datetime.now(TZ).strftime('%d/%m/%Y %H:%M')}\n",
         f"Checadas **{checadas}** vagas. Mudanças desde a última vez: "
         f"**{len(mudancas)}**\n"]
    L.append(f"(Total com status conhecido: {len(estado)}. A primeira checagem de "
             f"uma vaga registra o baseline e não conta como mudança.)\n")
    if mudancas:
        L.append("| Vaga | Antes | Agora |")
        L.append("|---|---|---|")
        for m in mudancas:
            L.append(f"| {m['titulo']} | {m['de'] or '(novo)'} | **{m['para']}** |")
    else:
        L.append("Nada mudou desde a última checagem.\n")
    L.append(f"\nDetalhe por vaga: `monitoramento.json`")
    SAIDA.write_text("\n".join(L) + "\n", encoding="utf-8")

    print(json.dumps({"monitoradas": checadas, "mudancas": len(mudancas)},
                     ensure_ascii=False))
    for m in mudancas:
        print(f"  {m['de'] or '(novo)'} -> {m['para']}: {m['titulo'][:52]}")
    print(f"-> {SAIDA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
