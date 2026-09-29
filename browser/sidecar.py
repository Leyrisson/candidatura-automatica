#!/usr/bin/env python3
"""sidecar.py — serviço HTTP de automação de browser para o pipeline de vagas.

Usado pelo n8n (HTTP Request → http://<host>:8787/...). Mantém UM perfil Firefox
persistente (user_data_dir) reutilizado em todas as sessões, então os portais onde
você já logou continuam logados — sem guardar senha.

Endpoints:
  POST /vaga    {"url": "..."}  → extrai título/texto/links da página da vaga
  POST /sessao  {"url": "...", "esperar": "linkedin"} → abre navegador VISÍVEL p/
                                 você logar (1x por portal); salva cookies sozinho
  GET  /health
  POST /logoff  zera o perfil (trocar de conta / abrir sessões de novo)
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.environ.get("VAGAS_SIDE_HOST", "0.0.0.0")
PORT = int(os.environ.get("VAGAS_SIDE_PORT", "8788"))
BASE = Path(__file__).resolve().parent
HEADLESS = os.environ.get("VAGAS_HEADLESS", "0") == "1"
LOGIN_MAX_SEG = int(os.environ.get("VAGAS_LOGIN_MAX_SEG", "240"))
# Browser fixo: Firefox. Chromium/Chrome NÃO é usado (sem VAGAS_BROWSER escolhendo
# engine — Chromium também bloqueia portais como gupy com ERR_ABORTED).
BROWSER = "firefox"
PROFILE = BASE / "perfil-ff"

# domínios dos portais — usados p/ detectar login concluído no /sessao
PORTAIS = {
    "gupy": ("gupy", ["gupy.io"]),
    "infojobs": ("infojobs", ["infojobs.com.br"]),
    "catho": ("catho", ["catho.com.br"]),
    "linkedin": ("linkedin", ["linkedin.com"]),
    "vagas": ("vagas", ["vagas.com.br", "vagas.com"]),
    "glassdoor": ("glassdoor", ["glassdoor.com", "glassdoor.com.br"]),
}
DOWNLOAD = BASE / "downloads"

# Ponte por arquivos com o pipeline (container) — HTTP container→host é
# bloqueado por firewall; o volume /workflows é compartilhado.
FILA_VAGAS = BASE.parent / "fila-vagas.jsonl"        # pipeline (container) enfileira
RESULTADOS = BASE.parent / "resultado-vagas.jsonl"   # sidecar (host) grava aqui
VAGAS_DB = Path(os.environ.get("VAGAS_DB", BASE.parent.parent / "vagas.db"))
WATCH_SEG = int(os.environ.get("VAGAS_WATCH_SEG", "5"))
ESTADO = BASE.parent / "estado.json"                  # bandeira do botão do widget

try:
    from lock_perfil import perfil_exclusivo as _perfil_exclusivo
except ImportError:  # roda de outro cwd
    import sys as _sys; _sys.path.insert(0, str(BASE))
    from lock_perfil import perfil_exclusivo as _perfil_exclusivo


def automacao_ligada():
    """Abandeira escrita por `alternar.py` (botão do widget).

    Desligado, o watcher não processa NADA da fila. É a segunda camada: a
    primeira é o `alternar.py` parar o serviço. Se o arquivo sumir ou ficar
    corrompido, trata como DESLIGADO — na dúvida o pior erro é candidatar
    para vaga errada.
    """
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8")).get("ativo") is True
    except (OSError, ValueError):
        return False
# 1 = envia candidatura de verdade nos portais com fluxo automatizável (infojobs).
# 0 = só lê a página e marca pronta_para_candidatar.
VAGAS_ENVIAR = os.environ.get("VAGAS_ENVIAR", "0") == "1"

# Palavras-frase dos botões de "candidatar-se" por portal (heurística de leitura;
# confirmação real de submissão é feita em _candidata_infofjobs, acima).
BOTOES_CANDIDATURA = (
    r"candidat[ae]r(?:-se|-me| se| me)?|inscrev[ae]r(?:-se|-me| se| me)?|aplicar|"
    r"apply now|apply for this job|candidate-se agora|enviar candidatura|candidatura gratuita"
)
MARCADORES_FECHADA = (
    r"vaga encerrada|oportunidade encerrada|prazo esgotado|"
    r"n[ãa]o estamos mais recebendo|candidatura encerrada|expirou"
)
MARCADORES_JA_CANDIDATADO = (
    r"voc[êe] j[áa] se candidatou|j[áa] est[áa] candidato|j[áa] se candidatou para esta vaga"
)
MARCADORES_EXITO = (
    r"candidatura enviada|candidatura registrada|candidatura conclu[íi]da|"
    r"voc[êe] se candidatou|candidatou-se a esta vaga|inscri[çc][ãa]o realizada|"
    r"candidatura aceita|candidatura de sucesso|"
    r"sua candidatura foi enviada|candidatura enviada com sucesso|voc[êe] [ée] um [bc]om match|"
    r"cv foi enviado|cv enviado"
)

# --- Conferência dos critérios NA PÁGINA (2026-09-27) -----------------------
# O e-mail de digest (InfoJobs/Catho) traz só cargo, empresa e cidade: o regime
# de contratação não aparece, então o classificador não consegue aplicá-lo e
# marcava regime "?" (confirmado pelo usuário: o check do CLT fica na página).
# A página é a autoridade e já está sendo aberta para candidatura — então é aqui que o
# regime e a exigência são conferidos, ANTES de clicar em candidatar-se.
# O teste de 2026-09-27 mostrou que o LLM local (2B e 3B) aprovava justamente
# as vagas de PJ e as que exigiam ensino superior, então não pode ser a autoridade.
REGIME_NA_PAGINA_RUIM = re.compile(
    r"pessoa\s*jur[ií]dica|\bpj\b|prestador\s*de\s*servi[çc]|contrata[çc][ãa]o\s+como\s+pj|"
    r"aut[ôo]nomo|freela(ncer|ance|nte)|tempor[áa]ri[oa]|contrato\s+determinado|"
    r"est[áa]gi[óo]|estagi[áa]ri[oa]|aprendiz|estágio|estagio",
    re.I,
)
EXIGENCIA_NA_PAGINA = re.compile(
    r"(exig|requer|necess[áa]ri|obrigat[óo]ri|comprov)[a-zçãáéíóúâêô]{0,3}\s*"
    r"(o\s+|a\s+)?(ensino\s+superior|curso\s+superior|gradua[çc][ãa]o|bacharel|"
    r"licenciatura|tecn[óo]logo|ensino\s+t[ée]cnico)|"
    # certificação exigida: exige o verbo E o nome da certificação. "Conhecimento
    # em CCNA" (sem verbo de exigência) é diferencial, não requisito — por isso
    # não entra aqui.
    r"(exig|requer|necess[áa]ri|obrigat[óo]ri|comprov)[a-zçãáéíóúâêô]{0,3}\s*"
    r"(o\s+|a\s+)?(certifica\w*|certificado|certifica[çc][ãa]o|ccna|comptia|cissp|"
    r"itil|pmp|aws\s+certified|azure\s+(fundamentals|administrator|developer))|"
    # verbo DEPOIS do nome: "Certificação Microsoft é obrigatória", "Diploma de
    # graduação necessário". `[^.]{0,40}` impede atravessar a frase seguinte.
    r"(certifica\w*|certificado|diploma|curso)\s+[^.]{0,40}?"
    r"(obrigat[óo]ri[oa]?|indispens[áa]vel|necess[áa]ri[oa]|exig[íi]d[oa]?|"
    r"[ée]\s+(obrigat[óo]ri[oa]|necess[áa]ri[oa]))|"
    r"(ensino\s+superior|curso\s+superior|gradua[çc][ãa]o)\s+(completo|conclu[íi]do|obrigat)",
    re.I,
)
# A negação ("não exige", "dispensável", "ensino médio é o básico") anula o
# match de exigência: limpa o texto antes de testar.
NEGACAO_EXIGENCIA = re.compile(
    r"(n[ãa]o\s+(exig|é\s+exigid|é\s+obrigat|precisa)|disp[ée]nsel|sem\s+"
    r"(exig|necess[áa]ri)|opcional|desej[áa]vel|not\s+required|"
    r"ensino\s+(m[ée]dio|fundamental)\s+([ée]\s+)?(suficiente|complet|o\s+b[áa]sico))",
    re.I,
)


# Confirmação POSITIVA de CLT. Decisão do usuário: só candidatar se a página
# confirmar o regime. A ausência de qualquer menção não é aprovação — vira
# "não confirmado" e a vaga fica para revisão manual.
REGIME_CLT_NA_PAGINA = re.compile(
    r"\bclt\b|regime\s*clt|contrata[çc][ãa]o\s*clt|emprego\s+fixo|sal[aá]rio\s+fixo|"
    r"efetivo|carteira\s+assinada|vt\s*(?:\+|e|&)?\s*vr|"
    # "CLT (Consolidação das Leis do Trabalho)" / "CLT - regime celetista"
    r"regime\s*celetista|consolida[çc][ãa]o\s+das\s+leis\s+do\s+trabalho",
    re.I,
)
# Menções que parecem CLT mas não são (falsos positivos do padrão acima).
REGIME_CLT_NAO_CONFIRMA = re.compile(
    r"\bn[ãa]o\s+(é|e)\s+(clt|efetivo)\b|sem\s+(clt|vt)\b|n[ãa]o\s+oferece\s+vt",
    re.I,
)


def confere_criterios_pagina(texto):
    """Confere regime e exigência no texto VISÍVEL da página da vaga.

    Devolve {"ok": bool, "motivo": str, "regime": str, "exigencia": str}.

    Ordem: sinal explícito de PJ/temporário/estágio reprova; exigência de
    superior/certificação reprova; e o regime tem de estar CONFIRMADO como
    CLT. Página que não fala de regime não reprova — devolve
    "regime_nao_confirmado" para a vaga ir para revisão manual em vez de ser
    descartada em silêncio (2026-09-27).
    """
    t = texto or ""
    m_reg = REGIME_NA_PAGINA_RUIM.search(t)
    if m_reg:
        return {"ok": False, "motivo": f"regime incompativel na pagina ({m_reg.group(0).strip()})",
                "regime": "incompativel", "exigencia": ""}
    limpo = NEGACAO_EXIGENCIA.sub(" ", t)
    m_exi = EXIGENCIA_NA_PAGINA.search(limpo)
    if m_exi:
        return {"ok": False, "motivo": f"exigencia na pagina ({m_exi.group(0).strip()[:60]})",
                "regime": "", "exigencia": "superior"}
    limpo_reg = REGIME_CLT_NAO_CONFIRMA.sub(" ", t)
    m_clt = REGIME_CLT_NA_PAGINA.search(limpo_reg)
    if m_clt:
        return {"ok": True, "motivo": "", "regime": "CLT", "exigencia": ""}
    return {"ok": False, "motivo": "regime nao confirmado na pagina (revisar antes de candidatar)",
            "regime": "?", "exigencia": "", "revisar": True}

# Requisições concorrentes subindo browser no MESMO perfil quebram o launch.
# Serializa /vaga, /sessao e /logoff.
_BROWSER_LOCK = threading.Lock()


def _lancador(p):
    """Launcher de browser — sempre Firefox."""
    return p.firefox


def _env_browser():
    """Env para o browser: força o Playwright Firefox a usar X11 (não wayland)."""
    e = os.environ.copy()
    e["MOZ_ENABLE_WAYLAND"] = "0"
    e.pop("WAYLAND_DISPLAY", None)
    return e


def _blindar_ctx(ctx):
    """Mascara sinais de automação no contexto (anti-detecção p/ Gupy etc.)."""
    for script in (
        # some browsers/CF checam navigator.webdriver
        "Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => undefined});",
        "try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}",
        # window.chrome ausente no Firefox chama atenção em alguns fingerprinters
        "try { window.chrome = window.chrome || { runtime: {} }; } catch (e) {}",
    ):
        try:
            ctx.add_init_script(script)
        except Exception as e:
            print(f"blindar: {e}", flush=True)


def html_para_texto(html):
    if not html:
        return ""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</(p|div|li|h[1-6]|tr|td)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
        t = t.replace(a, b)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def tratar_vaga(page):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(2500)
    except Exception:
        pass
    title = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    conteudo = html_para_texto(page.content())
    links = []
    try:
        for a in page.locator("a").all()[:60]:
            href = a.get_attribute("href") or ""
            txt = (a.inner_text() or "").strip()
            txt = re.sub(r"\s+", " ", txt)
            if txt and href and href not in ("#", "/") and "javascript:" not in href:
                links.append({"texto": txt[:80], "href": href})
    except Exception:
        pass
    return {"title": title[:300], "url": page.url, "texto": conteudo[:12000], "links": links}


def abrir(url):
    from playwright.sync_api import sync_playwright
    # Trava de perfil (entre processos) + trava de thread (dentro deste processo).
    with _perfil_exclusivo("sidecar /vaga"), _BROWSER_LOCK:
        with sync_playwright() as p:
            ctx = _lancador(p).launch_persistent_context(
                str(PROFILE), headless=HEADLESS, env=_env_browser(),
                viewport={"width": 1400, "height": 900}, locale="pt-BR",
            )
            _blindar_ctx(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                resp = {"erro": str(e)}
                resp.update(tratar_vaga(page))
                ctx.close()
                return resp
            resp = tratar_vaga(page)
            ctx.close()
            return resp


def sessao_interativa(url, alvo):
    """Abre navegador visível e MANTÉM aberto até você logar ou fechar a janela.

    NÃO fecha sozinho por "cookie de sessão": portais setam cookies anônimos
    (_ga, g_state, etc.) na primeira visita — confiá-los faria a janela fechar
    sem o login. A janela permanece até o fim do LOGIN_MAX_SEG, ou até você
    fechá-la (ctx fecha → detectamos). O perfil persistente salva tudo.
    """
    dom = (PORTAIS.get(alvo) or (None, []))[1] or ["_"]
    from playwright.sync_api import sync_playwright
    with _BROWSER_LOCK:
        with sync_playwright() as p:
            ctx = _lancador(p).launch_persistent_context(
                str(PROFILE), headless=False, env=_env_browser(),
                viewport={"width": 1400, "height": 900}, locale="pt-BR",
            )
            _blindar_ctx(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            ini = time.time()
            fechou = False
            while time.time() - ini < LOGIN_MAX_SEG:
                try:
                    if ctx.is_closed() or page.is_closed():
                        fechou = True
                        break
                except Exception:
                    fechou = True
                    break
                time.sleep(3)
            try:
                cookies_final = ctx.cookies()
            except Exception:
                cookies_final = []
            try:
                ctx.close()
            except Exception:
                pass
            return {"status": "fechado_pelo_usuario" if fechou else "tempo_esgotado",
                    "janela_aberta_seg": int(time.time() - ini),
                    "portais_com_cookie": sorted({c.get("domain", "") for c in cookies_final})}


def logoff():
    import shutil
    with _BROWSER_LOCK:
        if PROFILE.exists():
            shutil.rmtree(str(PROFILE))
        PROFILE.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Watcher da fila de vagas (ponte por arquivos com o pipeline no container).
# ---------------------------------------------------------------------------
def _detecta_portal(url):
    for nome, (_, dom) in PORTAIS.items():
        if any(dom and d in (url or "") for d in dom):
            return nome
    return ""


def _agora_iso():
    """Timestamp local ISO-8601 — o resultado-vagas.jsonl não tem coluna de
    data, então o carimbo sai junto com o resultado para o histórico por dia."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _le_fila():
    try:
        texto = FILA_VAGAS.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(l) for l in texto.splitlines() if l.strip()]


def _resultados_existentes():
    try:
        texto = RESULTADOS.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(l) for l in texto.splitlines() if l.strip()]


def _grava_resultado(r):
    with RESULTADOS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _atualiza_db(message_id, origem, etapa, pagina, detalhe="", subject=""):
    """Grava o resultado no vagas.db.

    Rodadas enfileiradas à mão (fila-vagas.jsonl com messageId sintético, ex.:
    "rod2-01") não têm linha em `vagas` — antes o UPDATE não casava com nada e a
    candidatura sumia do histórico. Por isso o INSERT OR IGNORE antes do UPDATE:
    o evento vira registro mesmo sem e-mail de origem (contador por portal/dia
    depende disso)."""
    if not message_id:
        return
    try:
        con = sqlite3.connect(VAGAS_DB, timeout=15)
        con.execute("PRAGMA busy_timeout=10000")
        try:
            con.execute("ALTER TABLE vagas ADD COLUMN pagina TEXT")
        except sqlite3.OperationalError:
            pass  # coluna já existe
        con.execute(
            "INSERT OR IGNORE INTO vagas (message_id, subject, link, etapa, origem, "
            "criado_em, atualizado_em) VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
            (message_id, (subject or "")[:300], origem or "", etapa, origem or ""))
        con.execute(
            "UPDATE vagas SET origem=?, etapa=?, pagina=?, atualizado_em=datetime('now') "
            "WHERE message_id=?", (origem or "", etapa, (pagina or "")[:12000], message_id))
        con.execute(
            "INSERT INTO etapas (vaga_id, etapa, detalhe, criado_em) "
            "SELECT id, ?, ?, datetime('now') FROM vagas WHERE message_id=?",
            (etapa, (detalhe or origem or "")[:400], message_id))
        con.commit()
        con.close()
    except Exception as e:
        print(f"watcher: db update falhou ({message_id}): {e}", flush=True)


_perfil = None


def _carrega_perfil():
    global _perfil
    if _perfil is None:
        try:
            _perfil = json.loads((BASE / "perfil.json").read_text(encoding="utf-8"))
        except Exception:
            _perfil = {}
    return _perfil


def _texto_visivel(page):
    try:
        return re.sub(r"\s+", " ", page.locator("body").inner_text())
    except Exception:
        return ""


def _resposta_para_pergunta(pergunta):
    """Resposta de perfil para uma pergunta do questionário (ou None se fora do perfil)."""
    q = re.sub(r"[^a-z0-9 ]", " ", pergunta.lower())
    for regra in _carrega_perfil().get("respostas_por_palavra", []):
        if any(p in q for p in regra.get("match", [])):
            return regra.get("resposta", "")
    faixa = _carrega_perfil().get("faixa_salarial")
    if faixa and "salarial" in pergunta.lower():
        return faixa
    return None


def _extrai_perguntas(texto):
    """Perguntas do questionário InfoJobs (entre o intro e o botão concluir)."""
    i = texto.find("responda as seguintes perguntas")
    j = texto.find("CONCLUIR CANDIDATURA")
    if i < 0 or j <= i:
        return []
    bloco = texto[i:j]
    if ": " in bloco:
        bloco = bloco.split(": ", 1)[1]
    partes = [p.strip() for p in re.split(r"Caracteres:", bloco)]
    perguntas = []
    for p in partes:
        p = re.sub(r"^\s*\d[\d.,]*\s*de\s*\d[\d.,]*\s*", "", p or "").strip()
        if p:
            perguntas.append(p)
    return perguntas[:5]


def _infofjobs_avanca(page):
    """Clique no 'Candidatar-me' e conclui o questionário quando as perguntas
    forem respondíveis pelo perfil.json (regra: parar e perguntar se faltar).

    NÃO inventa resposta fora do perfil (regra do usuário).
    Retorna dict: {"etapa": ..., "perguntas": [...]}."""
    link = None
    try:
        cand = page.locator("a.js_btApply, a.js_btApplyVacancy, a[class*='js_btApply']").first
        if cand.count() and cand.is_visible():
            link = cand
    except Exception:
        link = None
    if link is None:
        try:
            cand = page.get_by_text(re.compile(r"^candidatar(?:-me|-se)?\b", re.I))
            if cand.count():
                link = cand.first
        except Exception:
            link = None
    if link is None:
        return {"etapa": "sem_botao", "perguntas": []}
    try:
        link.click(timeout=12000)
    except Exception as e:
        return {"etapa": f"clique_falhou:{type(e).__name__}", "perguntas": []}
    fim = time.time() + 25
    while time.time() < fim:
        texto = _texto_visivel(page)
        if re.search(MARCADORES_EXITO, texto, flags=re.I):
            return {"etapa": "candidatado", "perguntas": []}
        if re.search(MARCADORES_JA_CANDIDATADO, texto, flags=re.I):
            return {"etapa": "ja_candidatado", "perguntas": []}
        if "CONCLUIR CANDIDATURA" in texto or "responda as seguintes perguntas" in texto:
            perguntas = _extrai_perguntas(texto)
            campos = []
            for fld in page.locator("textarea, input[type=text]").all():
                try:
                    if fld.is_visible() and (fld.get_attribute("name") or "").startswith("Item1"):
                        campos.append(fld)
                except Exception:
                    continue
            respostas = []
            faltando = []
            for q in perguntas:
                if not q.strip():
                    continue
                ans = _resposta_para_pergunta(q)
                if ans is None:
                    faltando.append(q)
                else:
                    respostas.append(ans)
            if faltando or len(respostas) != len(campos):
                return {"etapa": "exige_questionario",
                        "perguntas": [q for q in perguntas if q.strip()]}
            for fld, resp in zip(campos, respostas):
                try:
                    fld.fill(resp)
                except Exception:
                    try:
                        fld.type(resp, delay=15)
                    except Exception:
                        pass
            try:
                page.get_by_text("CONCLUIR CANDIDATURA").first.click(timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            continue
        time.sleep(1)
    texto = _texto_visivel(page)
    if re.search(MARCADORES_EXITO, texto, flags=re.I):
        return {"etapa": "candidatado", "perguntas": []}
    return {"etapa": "sem_confirmação", "perguntas": []}


def _campos_visiveis(page):
    """Campos de texto visíveis no form (exclui ocultos)."""
    out = []
    for fld in page.locator("textarea, input[type=text], input[type=number]").all():
        try:
            if fld.is_visible() and fld.get_attribute("type") not in ("hidden",):
                out.append(fld)
        except Exception:
            continue
    return out


def _label_do_campo(fld):
    """Tentativa de inferir a pergunta/rotulo associado a um campo."""
    try:
        for attr in ("aria-label", "placeholder"):
            v = fld.get_attribute(attr)
            if v:
                return v.strip()
    except Exception:
        pass
    try:
        v = fld.locator("xpath=preceding-sibling::*[1]").inner_text().strip()
        if v:
            return v
    except Exception:
        pass
    try:
        v = fld.evaluate("el => (el.closest('label')||{}).innerText || ''")
        if v:
            return v.strip()
    except Exception:
        pass
    try:
        v = fld.evaluate("""el => {
            const cont = el.closest('.form-group, fieldset, .field, .row, .modal-body') || el.parentElement || el;
            const t = (cont.innerText || '').replace(/\\s+/g, ' ').trim();
            const val = (el.value || '').trim();
            return t.replace(val, '').replace(/\\*$/, '').trim().slice(0, 200);
        }""")
        if v:
            return v
    except Exception:
        pass
    return ""


def _clica_por_texto(page, alvos, espera=2000):
    """Clica no primeiro alvo visível (case-insensitive, parcial)."""
    for alvo in alvos:
        try:
            el = page.get_by_text(alvo, exact=False).first
            if el.count() and el.is_visible():
                el.click(timeout=8000)
                page.wait_for_timeout(espera)
                return True
        except Exception:
            continue
    return False


def _avanca_candidatura(page, portal):
    """Fluxo genérico de candidatura extra-InfoJobs (catho/vagas/gupy).

    Clica no botão de candidatar-se, responde campos de texto cuja pergunta
    bater no perfil, clica em enviar/concluir e confirma pelo texto visível.
    Regra: pergunta fora do perfil → exige_questionario (para e pergunta)."""
    if portal == "infojobs":
        return _infofjobs_avanca(page)
    if re.search(r"cv (foi )?enviado|candidatura enviada", _texto_visivel(page), re.I):
        return {"etapa": "candidatado", "perguntas": []}
    if portal in ("vagas",) and re.search(
            r"tenho interesse (nessa vaga|nesta vaga)", _texto_visivel(page), re.I):
        return {"etapa": "externa", "perguntas": []}
    botoes = {
        "catho": ["Candidate-se", "Candidatar-se", "Candidatar-me", "Quero me candidatar"],
        "vagas": ["Candidatar-se", "Quero me candidatar", "Candidate-se"],
        "gupy": ["Quero me candidatar", "Candidatar-se", "Candidate-se"],
        "glassdoor": [],
        "linkedin": [],
    }.get(portal, ["Candidatar-se", "Candidate-se", "Quero me candidatar"])
    if not _clica_por_texto(page, botoes):
        return {"etapa": "sem_botao", "perguntas": []}
    fim = time.time() + 28
    while time.time() < fim:
        texto = _texto_visivel(page)
        if re.search(MARCADORES_EXITO, texto, re.I):
            return {"etapa": "candidatado", "perguntas": []}
        if re.search(MARCADORES_JA_CANDIDATADO, texto, re.I):
            return {"etapa": "ja_candidatado", "perguntas": []}
        campos = _campos_visiveis(page)
        if campos:
            fills, faltando = {}, []
            for fld in campos[:6]:
                q = _label_do_campo(fld)
                ans = _resposta_para_pergunta(q) if q else None
                if ans:
                    fills[fld] = ans
                elif q:
                    faltando.append(q)
            if faltando:
                return {"etapa": "exige_questionario", "perguntas": faltando[:5]}
            for fld, val in fills.items():
                try:
                    fld.fill(val)
                except Exception:
                    try:
                        fld.type(val, delay=15)
                    except Exception:
                        pass
            if _clica_por_texto(page, ["Enviar meu currículo", "Enviar candidatura",
                                       "Enviar minha candidatura", "Finalizar candidatura",
                                       "Enviar currículo", "Concluir", "Enviar"],
                               espera=1600):
                continue
        time.sleep(1)
    texto = _texto_visivel(page)
    if re.search(MARCADORES_EXITO, texto, re.I):
        return {"etapa": "candidatado", "perguntas": []}
    return {"etapa": "sem_confirmação", "perguntas": []}


def _candidar_portal(portal, url):
    """Abre a vaga e aplica no portal com fluxo automatizável (infojobs/catho/vagas)."""
    if portal == "infojobs":
        return _candidata_infojobs(url)
    from playwright.sync_api import sync_playwright
    with _BROWSER_LOCK:
        with sync_playwright() as p:
            ctx = _lancador(p).launch_persistent_context(
                str(PROFILE), headless=HEADLESS, env=_env_browser(),
                viewport={"width": 1400, "height": 900}, locale="pt-BR")
            _blindar_ctx(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                try:
                    ctx.close()
                except Exception:
                    pass
                return {"etapa": f"goto_falhou:{type(e).__name__}", "perguntas": []}
            try:
                page.wait_for_timeout(3500)
            except Exception:
                pass
            try:
                texto = _texto_visivel(page)
            except Exception:
                texto = ""
            if re.search(MARCADORES_FECHADA, texto, flags=re.I):
                estado, perguntas = "encerrada", []
            elif re.search(MARCADORES_JA_CANDIDATADO, texto, flags=re.I):
                estado, perguntas = "ja_candidatado", []
            else:
                res = _avanca_candidatura(page, portal)
                estado = res.get("etapa", "")
                perguntas = res.get("perguntas") or []
            try:
                ctx.close()
            except Exception:
                pass
            return {"etapa": estado, "perguntas": perguntas}


def _processa_vaga(url, msg, id_vaga, subject):
    """Abre a vaga num browser próprio; lê a página e, para portais com
    VAGAS_ENVIAR=1, aplica de verdade. Retorna dict de resultado."""
    from playwright.sync_api import sync_playwright
    with _BROWSER_LOCK:
        with sync_playwright() as p:
            ctx = _lancador(p).launch_persistent_context(
                str(PROFILE), headless=HEADLESS, env=_env_browser(),
                viewport={"width": 1400, "height": 900}, locale="pt-BR")
            _blindar_ctx(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            erro = ""
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                erro = f"{type(e).__name__}: {e}"
            pg = tratar_vaga(page)
            texto = pg.get("texto", "")
            texto_vis = _texto_visivel(page)
            links_txt = " ".join(l.get("texto", "") for l in pg.get("links", []))
            try:
                ctx.close()
            except Exception:
                pass
    portal = _detecta_portal(pg.get("url", url)) or _detecta_portal(url)
    tem_botao = bool(re.search(BOTOES_CANDIDATURA, texto + " " + links_txt, flags=re.I))
    etapa = "pronta_para_candidatar" if tem_botao else "pagina_lida"
    enviado = False
    detalhe = ""
    perguntas = []
    if portal == "vagas" and re.search(r"tenho interesse (nessa|nesta) vaga",
                                       texto_vis, flags=re.I):
        etapa = "externa"
        tem_botao = False
        detalhe = "vaga externa (vai p/ site da empresa)"
    elif re.search(MARCADORES_FECHADA, texto_vis, flags=re.I):
        etapa = "encerrada"
        tem_botao = False
    elif re.search(r"cv (foi )?enviado|candidatura enviada|j[áa] se candidatou",
                   texto_vis, flags=re.I):
        etapa = "candidatado"
        tem_botao = False
        enviado = True
        detalhe = "envio=ja_visto"
    elif re.search(MARCADORES_JA_CANDIDATADO, texto_vis, flags=re.I):
        etapa = "ja_candidatado"
        tem_botao = False
    elif VAGAS_ENVIAR and portal in ("infojobs", "catho", "vagas") and tem_botao:
        # Autoridade final dos critérios: a PÁGINA, não o e-mail e não o LLM
        # local. O digest não traz o regime, e o teste de 2026-09-27 mostrou que
        # o LLM local aprovava justamente PJ e "exige superior" — então quem
        # bloqueia a candidatura ruim é esta conferência, aqui, com a página já
        # aberta e o texto visível em mãos.
        conf = confere_criterios_pagina(texto_vis)
        if not conf["ok"]:
            # "exige_superior" era o rótulo de TUDO que não era CLT, inclusive
            # "não confirmado" — a página dizia uma coisa e o registro dizia
            # outra (2026-09-27). Agora cada recusa tem o nome do motivo.
            if conf["regime"] == "incompativel":
                etapa = "regime_incompativel"
            elif conf["exigencia"]:
                etapa = "exige_superior" if conf["exigencia"] == "superior" else "exige_certificacao"
            else:
                etapa = "regime_nao_confirmado"
            tem_botao = False
            detalhe = conf["motivo"]
        else:
            res = _candidar_portal(portal, url)
            estado = res.get("etapa", "")
            etapa = estado if estado in ("candidatado", "encerrada", "ja_candidatado",
                                         "exige_questionario", "externa", "sem_confirmação") else "pronta_para_candidatar"
            enviado = estado == "candidatado"
            detalhe = f"envio={estado}"
            perguntas = res.get("perguntas") or []
            if perguntas:
                detalhe = "perguntas=" + " | ".join(perguntas[:4])
    else:
        perguntas = []
    r = {
        "messageId": msg,
        "idVaga": id_vaga,
        "url": pg.get("url", url),
        "portal": portal,
        "titulo": (pg.get("title") or subject or "")[:300],
        "tem_botao": tem_botao,
        "etapa": etapa,
        "enviado": enviado,
        "perguntas": perguntas,
        "erro": erro,
        "detalhe": detalhe,
        "ts": _agora_iso(),
    }
    if not erro:
        _atualiza_db(msg, r["url"], etapa, texto, detalhe, subject=subject or r["titulo"])
    return r


def _candidata_infojobs(url):
    """Abra a vaga e clique em candidatar-se no InfoJobs (sessão própria).

    Retorna dict {"etapa", "perguntas"}."""
    from playwright.sync_api import sync_playwright
    with _BROWSER_LOCK:
        with sync_playwright() as p:
            ctx = _lancador(p).launch_persistent_context(
                str(PROFILE), headless=HEADLESS, env=_env_browser(),
                viewport={"width": 1400, "height": 900}, locale="pt-BR")
            _blindar_ctx(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                try:
                    ctx.close()
                except Exception:
                    pass
                return {"etapa": f"goto_falhou:{type(e).__name__}", "perguntas": []}
            try:
                page.wait_for_timeout(3000)
            except Exception:
                pass
            try:
                texto = _texto_visivel(page)
            except Exception:
                texto = ""
            if re.search(MARCADORES_FECHADA, texto, flags=re.I):
                estado = "encerrada"
                perguntas = []
            elif re.search(MARCADORES_JA_CANDIDATADO, texto, flags=re.I):
                estado = "ja_candidatado"
                perguntas = []
            else:
                res = _infofjobs_avanca(page)
                estado = res.get("etapa", "")
                perguntas = res.get("perguntas") or []
            try:
                ctx.close()
            except Exception:
                pass
            return {"etapa": estado, "perguntas": perguntas}


def _processa_item(item):
    url = item.get("url") or ""
    msg = item.get("messageId") or ""
    if not url:
        return {"messageId": msg, "erro": "sem url", "ts": _agora_iso()}
    try:
        r = _processa_vaga(url, msg, item.get("idVaga"), item.get("subject"))
    except Exception as e:
        r = {"messageId": msg, "idVaga": item.get("idVaga"), "url": url,
             "erro": f"{type(e).__name__}: {e}", "ts": _agora_iso()}
    return r


def _chave(item):
    """Identidade de uma candidatura na fila.

    NÃO pode ser só o messageId: um digest de e-mail pode trazer mais de uma
    vaga, e aí entram várias linhas com o mesmo messageId e URLs diferentes.
    Deduplicar pelo messageId ignorava a 2ª vaga da mesma mensagem (2026-09-27).
    """
    return f"{item.get('messageId') or ''}|{(item.get('url') or '').split('#')[0]}"


def _watcher():
    processed = set()
    avisou = False
    while True:
        try:
            if not automacao_ligada():
                # Não zera `processed`: ao religar, o que já foi feito
                # continua feito (a chave também está no resultado-vagas.jsonl).
                if not avisou:
                    print("watcher: automação DESLIGADA (botão do widget) — fila parada",
                          flush=True)
                    avisou = True
                time.sleep(WATCH_SEG)
                continue
            avisou = False
            feitos = {_chave(r) for r in _resultados_existentes()}
            for item in _le_fila():
                msg = item.get("messageId") or ""
                chave = _chave(item)
                if not msg or chave in feitos or chave in processed:
                    continue
                print(f"watcher: processando vaga {msg}", flush=True)
                try:
                    # Trava de perfil ENTRE processos: a coleta direta sobe o
                    # mesmo Firefox (user_data_dir) e os dois juntos faziam o
                    # segundo launch morrer com TargetClosedError.
                    with _perfil_exclusivo(f"sidecar {msg}") as ok:
                        if not ok:
                            continue
                        r = _processa_item(item)
                    r["chave"] = chave
                    _grava_resultado(r)
                except Exception as e:
                    _grava_resultado({"messageId": msg, "chave": chave,
                                      "erro": f"{type(e).__name__}: {e}",
                                      "ts": _agora_iso()})
                processed.add(chave)
        except Exception as e:
            print(f"watcher: {e}", flush=True)
        time.sleep(WATCH_SEG)


def iniciar_watcher():
    t = threading.Thread(target=_watcher, daemon=True, name="watcher-vagas")
    t.start()
    print(f"watcher: fila={FILA_VAGAS} resultado={RESULTADOS} db={VAGAS_DB} a cada {WATCH_SEG}s", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _read(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else {}
        except Exception:
            return {}

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send({"ok": True, "perfil": str(PROFILE)})
        return self._send({"erro": "rota não existe"}, 404)

    def do_POST(self):
        try:
            data = self._read()
            if self.path.startswith("/vaga"):
                rota = data.get("url")
                if not rota:
                    return self._send({"erro": "url obrigatória"}, 400)
                return self._send(abrir(rota))
            if self.path.startswith("/sessao"):
                rota = data.get("url", "https://www.google.com")
                alvo = data.get("esperar", "")
                return self._send(sessao_interativa(rota, alvo))
            if self.path.startswith("/logoff"):
                return self._send(logoff())
            if self.path.startswith("/health"):
                return self._send({"ok": True})
            return self._send({"erro": "rota não existe"}, 404)
        except Exception as e:
            return self._send({"erro": f"{type(e).__name__}: {e}"}, 500)


if __name__ == "__main__":
    PROFILE.mkdir(parents=True, exist_ok=True)
    DOWNLOAD.mkdir(parents=True, exist_ok=True)
    iniciar_watcher()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"sidecar-vagas http://{HOST}:{PORT} perfil={PROFILE} headless={HEADLESS}", flush=True)
    srv.serve_forever()