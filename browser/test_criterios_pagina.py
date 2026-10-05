# Testes da checagem de critérios na página da vaga (2026-09-27).
# Rodar:  ./.venv/bin/python test_criterios_pagina.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pathlib import Path  # noqa: E402

import sidecar as S  # noqa: E402
from sidecar import _carrega_perfil  # noqa: E402

CASOS = [
    # (nome, texto, ok esperado, regime esperado)
    ("clt claro", "Vaga: Analista de Ti. Contratação CLT. Benefícios: VT e VR.", True, "CLT"),
    ("clt e efetivo", "Regime: CLT. Efetivo, carteira assinada.", True, "CLT"),
    ("clt por extenso", "Regime celetista conforme a legislação.", True, "CLT"),
    ("vt e vr", "Oferecemos VT e VR para a equipe.", True, "CLT"),
    ("pessoa juridica", "Contratação como pessoa jurídica.", False, "incompativel"),
    ("pj", "Contratação PJ.", False, "incompativel"),
    ("temporaria", "Vaga temporária, 6 meses.", False, "incompativel"),
    ("estagio", "Estágio em TI.", False, "incompativel"),
    ("aprendiz", "Vaga de aprendiz.", False, "incompativel"),
    # "exige superior" e "exige ccna": desde 2026-10-03 menção a exigência na
    # DESCRIÇÃO não reprova a vaga — a recusa é decidida pelo campo obrigatório
    # do formulário (`_campo_formacao_obrigatorio`), porque a maioria dos cards
    # do InfoJobs lista "Ensino Superior" como boilerplate sem que o formulário
    # cobre aquilo. Aqui não há regime nenhum na página, então o retorno é
    # ok=False com regime="?" (revisar). O esperado era "" — de antes da regra.
    ("exige superior", "Exige ensino superior completo.", False, "?"),
    ("exige ccna", "Certificação CCNA é obrigatória.", False, "?"),
    ("sem mencao a regime", "Vaga de Analista de Ti em São Paulo. Requisitos: experiência.", False, "?"),
    ("nega clt", "Não é CLT, contratação PJ.", False, "incompativel"),
    ("diferencial nao reprova", "Contratação CLT. Conhecimento em CCNA é diferencial.", True, "CLT"),
    ("ensino medio suficiente", "Contratação CLT. Ensino médio completo é suficiente.", True, "CLT"),
]


def teste_cache_perfil():
    """O cache do perfil.json tem de cair sozinho.

    Quem escreve o perfil.json é OUTRO processo — o telebot, pelo
    gravar-respostas.py, quando o dono responde uma pergunta de vaga. Com cache
    sem prazo, a regra nova só valia depois de reiniciar o serviço, que era
    exatamente o passo que se esquecia. Aqui: grava por fora, confere que o
    sidecar enxerga sem reiniciar."""
    import json
    import shutil
    import tempfile

    original = S.BASE / "perfil.json"
    with tempfile.TemporaryDirectory() as tmp:
        # BASE é o DIRETÓRIO do browser, não o arquivo: é dele que o sidecar
        # faz BASE / "perfil.json".
        falso_dir, falso = Path(tmp), Path(tmp) / "perfil.json"
        shutil.copy(original, falso)
        alvo, S.BASE = S.BASE, falso_dir
        S._perfil, S._perfil_mtime = None, None
        try:
            antes = len(_carrega_perfil()["respostas_por_palavra"])
            # outro processo acrescenta regra
            d = json.loads(falso.read_text(encoding="utf-8"))
            d["respostas_por_palavra"].append(
                {"match": ["regra escrita por outro processo"],
                 "resposta": "Sim"})
            falso.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            # o sidecar continua vivo, sem restart: tem que reler
            depois = _carrega_perfil()["respostas_por_palavra"]
            if len(depois) == antes:
                return False, "releu o cache velho: regra nova nao apareceu"
            if not any("outro processo" in m
                       for r in depois for m in r.get("match", [])):
                return False, "releu, mas sem a regra nova"
            # arquivo momentaneamente quebrado nao pode zerar o perfil
            falso.write_text("{ nao é json", encoding="utf-8")
            if len(_carrega_perfil()["respostas_por_palavra"]) != len(depois):
                return False, "perfil zerou com arquivo ilegivel"
        finally:
            S.BASE = alvo
            S._perfil, S._perfil_mtime = None, None
    return True, "releu do disco e aguentou arquivo quebrado"


def main():
    falhas = 0
    for nome, texto, ok_esp, regime_esp in CASOS:
        r = S.confere_criterios_pagina(texto)
        ok = r["ok"] == ok_esp and r["regime"] == regime_esp
        if not ok:
            falhas += 1
        print(
            f"{'OK ' if ok else 'XX '}{nome.ljust(24)} "
            f"ok={str(r['ok']):<6}regime={r['regime']:<13} {r['motivo'][:46]}"
        )

    ok, msg = teste_cache_perfil()
    if not ok:
        falhas += 1
    print(f"{'OK ' if ok else 'XX '}{'cache do perfil'.ljust(24)} {msg}")
    print()
    if falhas:
        print(f"{falhas} ERRO(S) em {len(CASES := CASOS) + 1} casos")
        return 1
    print(f"todos ok ({len(CASOS) + 1}/{len(CASOS) + 1})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
