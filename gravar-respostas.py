#!/usr/bin/env python3
"""Grava a resposta que o dono deu no Telegram para uma vaga.

Duas coisas bem diferentes podem entrar aqui, e por isso o script decide onde
cada uma vai:

- **Resposta da vaga** (sempre): vai para `browser/respostas-vaga.json`, com a
  chave sendo a pergunta normalizada. Garante que ESTA candidatura sai.
- **Resposta reutilizável** (às vezes): vai também para o `perfil.json`, em
  `respostas_por_palavra`, e passa a valer sozinha em qualquer vaga futura que
  pergunte a mesma coisa.

O que decide a promoção é a pergunta aberta ou fechada. "Aceita R$ 2.000?" é
fato seu, vale para sempre. "Conte uma situação em que você cortou custo" é
resposta daquela vaga — promover isso faria a próxima vaga levar o texto errado,
então fica preso nela.

Use --so-nesta-vaga ou --reutilizar para decidir na mão quando a heurística errar.

A chave tem de ser a mesma normalização que o sidecar usa em
`_sem_acento()`; se divergir, a vaga trava de novo em exige_questionario.
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent
BROWSER = BASE / "browser"
STORE = BROWSER / "respostas-vaga.json"
PERFIL = BROWSER / "perfil.json"

# Pergunta aberta é a que pede história/justificativa. Verbo no começo da frase.
ABERTA = re.compile(
    r"^\W*(conte|descreva|descreva|explique|relacione|comente|fale|"
    r"apresente|cite|descreva)\b", re.I)
# Valor monetário é SEMPRE da vaga, nunca regra do perfil. Aceitar R$ 2.000 na
# empresa A não significa aceitar R$ 1.500 na B: a resposta é sobre aquela
# oferta. Quem guarda isso no perfil é exatamente o bug de mandar "sim" para
# qualquer salário depois — o `faixa_salarial` do perfil já cobre o seu limite.
DINHEIRO = re.compile(
    r"(sal[aá]ri|remunera|valor|pretens|salarial|quanto (vai|recebe|paga|"
    r"gasta)|r\$|reais|di[aá]ri)", re.I)
# Não é pergunta, é contato/curriculum: o certo é enviar o arquivo, não digitar.
NAO_E_PERGUNTA = re.compile(
    r"(envie|anexe|attach|seu curriculo|seu currículo|currículo|curriculum)\b",
    re.I)
MAX_REUTILIZAVEL = 200      # acima disso é texto longo: e da vaga


def sem_acento(texto):
    """Mesma normalizacao do sidecar — precisa casar byte a byte."""
    s = unicodedata.normalize("NFKD", str(texto).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def chave_pergunta(pergunta):
    """A chave que o sidecar procura: texto normalizado e sem espaco sobrando."""
    return sem_acento(pergunta).rstrip("?")


def reutilavel(pergunta, resposta):
    if NAO_E_PERGUNTA.search(pergunta):
        return False, "pede anexo/contato, nao e resposta de texto"
    if DINHEIRO.search(pergunta):
        return False, "valor monetario e da oferta, nao regra do perfil"
    if ABERTA.match(pergunta):
        return False, "pergunta aberta (pede historia/justificativa)"
    if len(resposta) > MAX_REUTILIZAVEL:
        return False, f"resposta longa demais ({len(resposta)} letras)"
    return True, "resposta curta e pergunta fechada"


def termo_de_casamento(chave):
    """Trecho da pergunta vira chave de casamento no perfil — sem os numeros.

    Sem tirar o numero, "aceita o salario de r 2 000 00" viraria
    "aceita o salario de r 2", que casa com "Aceita R$ 2.500?" e faria o bot
    aceitar qualquer salario sozinho.
    """
    palavras = [p for p in chave.split() if not any(c.isdigit() for c in p)]
    return " ".join(palavras[:6]).strip()


def le_json(caminho, padrao):
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def grava(caminho, dados):
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(caminho)          # substitui de uma vez: nao deixa meio arquivo


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True, help="vaga que a resposta pertence")
    ap.add_argument("--pergunta", required=True)
    ap.add_argument("--resposta", required=True)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--reutilizar", action="store_true",
                   help="forca ir tambem para o perfil.json")
    g.add_argument("--so-nesta-vaga", action="store_true",
                   help="forca ficar preso nesta vaga")
    a = ap.parse_args()

    url = a.url.split("?")[0]
    chave = chave_pergunta(a.pergunta)
    resp = a.resposta.strip()
    if not chave:
        print(json.dumps({"ok": False, "erro": "pergunta vazia"}))
        return 1

    # 1) sempre na vaga
    store = le_json(STORE, {})
    vaga = store.setdefault(url, {})
    antes = vaga.get(chave)
    vaga[chave] = resp
    grava(STORE, store)

    # 2) Promocao: decide se entra tambem no perfil.json
    if a.so_nesta_vaga:
        promovel, motivo = False, "forcado por --so-nesta-vaga"
    else:
        promovel, motivo = reutilavel(a.pergunta, resp)
    if a.reutilizar:
        promovel, motivo = True, "forcado por --reutilizar"

    promovido = False
    if promovel:
        perfil = le_json(PERFIL, {})
        regras = perfil.setdefault("respostas_por_palavra", [])
        termo = termo_de_casamento(chave)
        if not termo:
            promovel, motivo = False, "pergunta so com numeros, sem termo fixo"
        elif any(termo in sem_acento(" ".join(r.get("match", [])))
                 for r in regras):
            motivo += " (regra ja existia no perfil)"
        else:
            regras.append({"match": [termo], "resposta": resp})
            grava(PERFIL, perfil)
            promovido = True

    print(json.dumps({
        "ok": True,
        "url": url,
        "chave": chave,
        "resposta": resp,
        "substituiu": antes if antes is not None else None,
        "reutilizavel": promovel,
        "promovido_ao_perfil": promovido,
        "motivo": motivo,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
