# Testes da checagem de critérios na página da vaga (2026-09-27).
# Rodar:  ./.venv/bin/python test_criterios_pagina.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sidecar as S  # noqa: E402

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
    print()
    if falhas:
        print(f"{falhas} ERRO(S) em {len(CASOS)} casos")
        return 1
    print(f"todos ok ({len(CASOS)}/{len(CASOS)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
