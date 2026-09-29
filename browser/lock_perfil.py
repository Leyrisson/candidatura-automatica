#!/usr/bin/env python3
"""Trava de perfil compartilhada ENTRE PROCESSOS.

O sidecar e o `coleta.py` sobem o Firefox com o mesmo `user_data_dir`. O
`_BROWSER_LOCK` do sidecar (threading.Lock) só vale DENTRO daquele processo, então
os dois juntos brigavam pelo perfil: o segundo `launch_persistent_context` morria
com `TargetClosedError` (2026-09-27).

Aqui a trava é um `flock` no sistema de arquivos, que o kernel respeita entre
processos. Firefox adquire um lock parecido no `.parentlock`, mas o erro dele
aparece como exceção genérica; este lock dá uma espera clara e nomeada.
"""
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

ARQ = Path(__file__).resolve().parent / "perfil.lock"


@contextmanager
def perfil_exclusivo(nome, espera_s=240, aviso_s=45):
    """Serializa o uso do perfil do Firefox entre processos.

    `nome` aparece na espera, para o log dizer quem está segurando. Não estoura
    exceção: ao estourar, devolve sem a trava e o chamador trata (a coleta
   prefere falhar e tentar na próxima rodada a se perder).
    """
    fh = open(ARQ, "a+")
    t0 = time.monotonic()
    avisou = False
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - t0 > espera_s:
                    print(f"[perfil] {nome}: espera esgotada ({espera_s}s) — "
                          f"saindo sem usar o perfil", flush=True)
                    fh.close()
                    yield False
                    return
                if not avisou and time.monotonic() - t0 > aviso_s:
                    print(f"[perfil] {nome}: aguardando o perfil "
                          f"(outro processo está no browser)...", flush=True)
                    avisou = True
                time.sleep(2)
        if avisou:
            print(f"[perfil] {nome}: pegou o perfil", flush=True)
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    finally:
        try:
            fh.close()
        except OSError:
            pass
