"""
Dashboard seguro: sincroniza gastos de Factorial → Holded.
Ejecutar: streamlit run app.py
"""
from __future__ import annotations

import os
import re
import json
import time
import base64
import hashlib
import hmac
import logging
import requests
import pandas as pd
import streamlit as st
from datetime import datetime, date as _date
from pathlib import Path
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()

# ── Logging de auditoría ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
_audit = logging.getLogger("audit")

# ── Secrets ───────────────────────────────────────────────────────────────────
def _secret(key: str) -> str | None:
    try:
        val = st.secrets[key]
        return str(val) if val else None
    except KeyError:
        return os.getenv(key) or None
    except Exception:
        return os.getenv(key) or None

FACTORIAL_API_KEY = _secret("FACTORIAL_API_KEY")
HOLDED_API_KEY    = _secret("HOLDED_API_KEY")
GITHUB_TOKEN      = _secret("GITHUB_TOKEN")
APP_PASSWORD      = _secret("APP_PASSWORD")

FACTORIAL_BASE = "https://api.factorialhr.com/api/2026-01-01/resources"
HOLDED_BASE    = "https://api.holded.com/api"

# Headers construidos bajo demanda — nunca globales (evita exposición en tracebacks)
def _f_headers() -> dict:
    return {"x-api-key": FACTORIAL_API_KEY or "", "Accept": "application/json"}

def _h_headers() -> dict:
    return {"key": HOLDED_API_KEY or "", "Accept": "application/json"}

DATA_DIR          = Path(__file__).parent / "data"
SYNCED_FILE       = DATA_DIR / "synced_expenses.json"
ATTACH_DIR        = DATA_DIR / "attachments"
LAST_FETCH_FILE   = DATA_DIR / "last_fetch.txt"
AUDIT_FILE        = DATA_DIR / "audit.log"
FISCAL_CACHE_FILE = DATA_DIR / "fiscal_names.json"
CITY_CACHE_FILE   = DATA_DIR / "fiscal_cities.json"

# Directorios con permisos restrictivos (solo el proceso puede leer/escribir)
DATA_DIR.mkdir(exist_ok=True, mode=0o700)
ATTACH_DIR.mkdir(exist_ok=True, mode=0o700)

MAX_ATTACH_BYTES   = 10 * 1024 * 1024  # 10 MB máximo por adjunto
SESSION_TIMEOUT_H  = 8                  # horas hasta expirar sesión automáticamente
MAX_LOGIN_ATTEMPTS = 3                  # intentos fallidos antes de bloqueo
LOCKOUT_SECONDS    = 300                # 5 minutos de bloqueo tras intentos fallidos

st.set_page_config(page_title="Gastos Factorial → Holded", page_icon="💸", layout="wide")


# ── SSRF: validación estricta de URLs para adjuntos ───────────────────────────
_EXACT_DOMAINS = frozenset({
    "factorialhr.com",
    "cdn.factorialhr.com",
    "storage.googleapis.com",
})

# Patrón para S3: bucket.s3.region.amazonaws.com o s3.region.amazonaws.com
_S3_RE = re.compile(r'^([\w-]+\.)?s3([.\-][\w-]+)*\.amazonaws\.com$', re.IGNORECASE)

def _is_allowed_url(url: str) -> bool:
    """Solo permite HTTPS hacia dominios conocidos de Factorial/S3/GCS."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower().strip(".")
        if not host or " " in host or len(host) > 253:
            return False
        if host in _EXACT_DOMAINS:
            return True
        if host.endswith(".amazonaws.com") and _S3_RE.match(host):
            return True
        return False
    except Exception:
        return False


# ── Rate limiting persistente (server-side, no bypasseable con nueva pestaña) ─
_LOCKOUT_FILE = DATA_DIR / ".lockout"

def _get_lockout_state() -> tuple[int, float]:
    """Devuelve (intentos_fallidos, lockout_until_timestamp) desde fichero."""
    if _LOCKOUT_FILE.exists():
        try:
            data = json.loads(_LOCKOUT_FILE.read_text())
            return int(data.get("fails", 0)), float(data.get("until", 0))
        except Exception:
            pass
    return 0, 0.0

def _set_lockout_state(fails: int, until: float):
    try:
        _LOCKOUT_FILE.write_text(json.dumps({"fails": fails, "until": until}))
    except Exception:
        _audit.error("No se pudo escribir estado de lockout")

def _clear_lockout():
    try:
        if _LOCKOUT_FILE.exists():
            _LOCKOUT_FILE.unlink()
    except Exception:
        pass


# ── Autenticación: contraseña obligatoria + rate limiting + expiración ────────
def _check_auth():
    # APP_PASSWORD siempre requerido — sin excepción
    if not APP_PASSWORD:
        st.error(
            "⚠️ **Configuración incompleta**: APP_PASSWORD no está definido.\n\n"
            "Agrega `APP_PASSWORD = \"tu-clave\"` en los Secrets de Streamlit Cloud "
            "o en el archivo `.env` para desarrollo local."
        )
        st.stop()

    now = time.time()

    # Expiración de sesión por inactividad
    if st.session_state.get("_auth_ok"):
        if now - st.session_state.get("_auth_ts", 0) > SESSION_TIMEOUT_H * 3600:
            st.session_state["_auth_ok"] = False
            st.session_state.pop("_auth_ts", None)
            st.info("Sesión expirada. Inicia sesión de nuevo.")
        else:
            return  # Sesión activa y vigente

    # Bloqueo server-side por intentos fallidos (persistente en fichero)
    fails, lockout_until = _get_lockout_state()
    if now < lockout_until:
        remaining = int(lockout_until - now)
        m, s = divmod(remaining, 60)
        st.title("💸 Gastos Factorial → Holded")
        st.error(f"🔒 Demasiados intentos fallidos. Espera **{m}m {s}s** antes de reintentar.")
        st.stop()

    # Formulario de login
    st.title("💸 Gastos Factorial → Holded")
    st.caption("Acceso restringido — solo personal autorizado")

    with st.form("login_form", clear_on_submit=True):
        pwd       = st.text_input("Contraseña", type="password", placeholder="Contraseña de acceso")
        submitted = st.form_submit_button("Entrar", type="primary", use_container_width=True)

    if submitted:
        # Comparación timing-safe para evitar timing attacks
        entered  = hashlib.sha256(pwd.encode()).digest()
        expected = hashlib.sha256(APP_PASSWORD.encode()).digest()
        if hmac.compare_digest(entered, expected):
            st.session_state["_auth_ok"]       = True
            st.session_state["_auth_ts"]       = now
            _clear_lockout()
            _audit.info("AUTH | LOGIN_OK")
            st.rerun()
        else:
            fails += 1
            _audit.warning(f"AUTH | LOGIN_FAIL | intento {fails}/{MAX_LOGIN_ATTEMPTS}")
            if fails >= MAX_LOGIN_ATTEMPTS:
                _set_lockout_state(0, now + LOCKOUT_SECONDS)
                st.error(f"🔒 Acceso bloqueado por {LOCKOUT_SECONDS // 60} minutos tras {MAX_LOGIN_ATTEMPTS} intentos fallidos.")
            else:
                _set_lockout_state(fails, 0)
                left = MAX_LOGIN_ATTEMPTS - fails
                st.error(f"Contraseña incorrecta. Quedan **{left}** intento(s).")
    st.stop()

_check_auth()


# ── Auditoría de operaciones ──────────────────────────────────────────────────
def _log_op(action: str, exp_id: str = "", desc: str = "", amount: float = 0.0):
    entry = (f"{datetime.utcnow().isoformat()}Z | {action}"
             f"{' | ' + exp_id if exp_id else ''}"
             f"{' | ' + desc[:40] if desc else ''}"
             f"{' | ' + str(round(amount, 2)) + 'EUR' if amount else ''}\n")
    try:
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        pass
    _audit.info(f"{action} | {exp_id} | {desc[:40]} | {round(amount, 2)}EUR")


# ── Auto-refresh diario ───────────────────────────────────────────────────────
def _should_auto_refresh() -> bool:
    if not LAST_FETCH_FILE.exists():
        return True
    try:
        last = datetime.fromisoformat(LAST_FETCH_FILE.read_text().strip())
        return (datetime.utcnow() - last).total_seconds() > 86400
    except Exception:
        return True

def _mark_fetched():
    LAST_FETCH_FILE.write_text(datetime.utcnow().isoformat())

if _should_auto_refresh():
    st.cache_data.clear()
    _mark_fetched()


# ── Persistencia: GitHub (cloud) + local (dev) ────────────────────────────────
_GH_REPO    = _secret("GH_REPO") or "facundh23/factorial-holded-expenses"
_GH_FILE    = "data/synced_expenses.json"
_GH_API_URL = f"https://api.github.com/repos/{_GH_REPO}/contents/{_GH_FILE}"

def _gh_headers() -> dict:
    return {"Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"}

def load_synced() -> dict:
    if GITHUB_TOKEN:
        try:
            r = requests.get(_GH_API_URL, headers=_gh_headers(), timeout=10)
            if r.status_code == 200:
                content = base64.b64decode(r.json()["content"]).decode()
                return json.loads(content)
        except Exception as e:
            _audit.warning(f"GITHUB | LOAD_FAIL | {type(e).__name__}")
    if SYNCED_FILE.exists():
        with open(SYNCED_FILE) as f:
            return json.load(f)
    return {"synced_ids": [], "last_sync": None}

def save_synced(synced_data: dict):
    synced_data["last_sync"] = datetime.utcnow().isoformat() + "Z"
    if GITHUB_TOKEN:
        try:
            encoded = base64.b64encode(json.dumps(synced_data, indent=2).encode()).decode()
            r       = requests.get(_GH_API_URL, headers=_gh_headers(), timeout=10)
            sha     = r.json().get("sha") if r.status_code == 200 else None
            payload = {"message": "sync: actualizar gastos sincronizados", "content": encoded}
            if sha:
                payload["sha"] = sha
            requests.put(_GH_API_URL, json=payload, headers=_gh_headers(), timeout=15)
        except Exception as e:
            _audit.error(f"GITHUB | SAVE_FAIL | {type(e).__name__}")
    tmp = SYNCED_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(synced_data, f, indent=2)
    tmp.replace(SYNCED_FILE)

def get_synced_ids() -> set:
    return set(str(i) for i in load_synced().get("synced_ids", []))


# ── Persistencia GitHub: fiscal_names.json ───────────────────────────────────
_GH_FISCAL_FILE = "data/fiscal_names.json"
_GH_FISCAL_URL  = f"https://api.github.com/repos/{_GH_REPO}/contents/{_GH_FISCAL_FILE}"

def _gh_load_fiscal() -> dict:
    if GITHUB_TOKEN:
        try:
            r = requests.get(_GH_FISCAL_URL, headers=_gh_headers(), timeout=10)
            if r.status_code == 200:
                return json.loads(base64.b64decode(r.json()["content"]).decode())
        except Exception:
            pass
    return {}

def _gh_save_fiscal(cache: dict):
    if not GITHUB_TOKEN:
        return
    try:
        encoded = base64.b64encode(json.dumps(cache, indent=2, ensure_ascii=False).encode()).decode()
        r = requests.get(_GH_FISCAL_URL, headers=_gh_headers(), timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {"message": "sync: actualizar nombres fiscales", "content": encoded}
        if sha:
            payload["sha"] = sha
        requests.put(_GH_FISCAL_URL, json=payload, headers=_gh_headers(), timeout=15)
    except Exception as e:
        _audit.error(f"GITHUB | FISCAL_SAVE_FAIL | {type(e).__name__}")


# ── Fetchers ──────────────────────────────────────────────────────────────────
@st.cache_data(ttl=86400, show_spinner=False)
def fetch_all_expenses() -> list:
    all_items, page = [], 1
    while True:
        params = {"include_attachments": "true", "per_page": 100, "page": page}
        r = requests.get(f"{FACTORIAL_BASE}/expenses/expenses",
                         headers=_f_headers(), params=params, timeout=30)
        if r.status_code != 200:
            break
        data  = r.json()
        items = data.get("data", [])
        if not isinstance(items, list):
            break
        all_items.extend(items)
        if not data.get("meta", {}).get("has_next_page", False):
            break
        page += 1
        time.sleep(0.2)
    return all_items

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_employees() -> dict:
    r = requests.get(f"{FACTORIAL_BASE}/employees/employees",
                     headers=_f_headers(), params={"per_page": 200}, timeout=30)
    if r.status_code != 200:
        return {}
    result = {}
    for emp in r.json().get("data", []):
        a    = emp.get("attributes", emp)
        eid  = str(emp.get("id") or a.get("id"))
        name = f"{a.get('first_name', '')} {a.get('last_name', '')}".strip()
        result[eid] = name or eid
    return result


# ── Mapeo categoría Factorial → cuenta contable Holded ───────────────────────
_CATEGORY_ACCOUNT: dict[str, str] = {
    "accommodation":       "62900014",  # Hoteles
    "airlines":            "62900019",  # Dietas viajes
    "car_rental_agencies": "62900017",  # Desplazamientos
    "fuel":                "62900020",  # Gasolina
    "office_supplies":     "62900001",  # Gastos Oficina
    "other":               "62900000",  # Otros servicios
    "parking":             "62900017",  # Desplazamientos
    "private_transport":   "62900020",  # Gasolina
    "public_transport":    "62900017",  # Desplazamientos
    "restaurants":         "62900015",  # Dietas
    "subscriptions":       "62700009",  # Plataforma
    "tolls":               "62900017",  # Desplazamientos
}
_DEFAULT_ACCOUNT = "62900000"  # Otros servicios (fallback)

# Mapeo por nombre de categoría (case-insensitive) como fallback
_CATEGORY_NAME_ACCOUNT: dict[str, str] = {
    "alojamiento":         "62900014",
    "hoteles":             "62900014",
    "accommodation":       "62900014",
    "aerolíneas":          "62900019",
    "vuelos":              "62900019",
    "airlines":            "62900019",
    "flights":             "62900019",
    "alquiler de coches":  "62900017",
    "car rental":          "62900017",
    "car_rental_agencies": "62900017",
    "combustible":         "62900020",
    "gasolina":            "62900020",
    "fuel":                "62900020",
    "material de oficina": "62900001",
    "office supplies":     "62900001",
    "office_supplies":     "62900001",
    "otros":               "62900000",
    "other":               "62900000",
    "parking":             "62900017",
    "aparcamiento":        "62900017",
    "transporte privado":  "62900020",
    "private transport":   "62900020",
    "private_transport":   "62900020",
    "transporte público":  "62900017",
    "public transport":    "62900017",
    "public_transport":    "62900017",
    "metro":               "62900017",
    "restaurantes":        "62900015",
    "restaurants":         "62900015",
    "dietas":              "62900015",
    "comidas":             "62900015",
    "meals":               "62900015",
    "suscripciones":       "62700009",
    "subscriptions":       "62700009",
    "peajes":              "62900017",
    "tolls":               "62900017",
    "transporte":          "62900017",
    "transport":           "62900017",
    "desplazamientos":     "62900017",
    "viajes":              "62900019",
    "travel":              "62900019",
}

_ACCOUNT_NAME: dict[str, str] = {
    "62700005": "Mat. Publicitario",
    "62700009": "Plataforma",
    "62800000": "Suministros",
    "62800002": "Sum. Teléfono & Internet",
    "62900000": "Otros servicios",
    "62900001": "Gastos Oficina",
    "62900012": "Gastos Dirección",
    "62900014": "Hoteles",
    "62900015": "Dietas",
    "62900017": "Desplazamientos",
    "62900019": "Dietas viajes",
    "62900020": "Gasolina",
}

# Mapeo cuenta contable → ID interno de Holded (necesario para la API)
_ACCOUNT_HOLDED_ID: dict[str, str] = {
    "62700005": "5ef0b09d6a97280dae40be85",  # Mat. Publicitario
    "62700009": "663c93e162a37e6b9a0cda08",  # Plataforma
    "62800000": "5e9ffd0e6a9728403d33d387",  # Suministros
    "62800002": "5edf97b36a972864067077b9",  # Sum. Teléfono & Internet
    "62900000": "5e9ffd0e6a9728403d33d388",  # Otros servicios
    "62900001": "5edf97b36a9728640670776c",  # Gastos Oficina
    "62900012": "5f06c2fa6a972860516dadb3",  # Gastos Dirección
    "62900014": "62849fb044c49bbdb401df52",  # Hoteles
    "62900015": "6343f53c55788d9b310cef1f",  # Dietas
    "62900017": "63623bd6d4447cc1eb0c03fc",  # Desplazamientos
    "62900019": "6371f82d86e1cea55303dec2",  # Dietas viajes
    "62900020": "6371f912ee02e06a4a065649",  # Gasolina
}
_DEFAULT_ACCOUNT_HOLDED_ID = "5e9ffd0e6a9728403d33d388"  # Otros servicios

# ── Mapeo método de pago Factorial → cuenta de pago Holded ──────────────────
_PAYMENT_ACCOUNT: dict[str, str] = {
    "factorial_card":        "57290016",   # Tarjeta gastos Factorial
    "corporate_credit_card": "57290016",   # Tarjeta corporativa
    "personal_debit_card":   "46500000",   # Reembolso pendiente empleado
}
_DEFAULT_PAYMENT_ACCOUNT = "57290016"

_PAYMENT_ACCOUNT_NAME: dict[str, str] = {
    "57290016": "Tarjeta Factorial",
    "46500000": "Reembolso empleado",
}

# ID de tesorería en Holded para registrar el pago
_TREASURY_ID: dict[str, str] = {
    "57290016": "69dd035eb3a31def26017001",  # Cuenta de tarjetas de gastos Factorial
}

def _get_payment_account(expense_attributes: dict) -> str:
    method = (expense_attributes.get("payment_method") or "").lower()
    return _PAYMENT_ACCOUNT.get(method, _DEFAULT_PAYMENT_ACCOUNT)

def _get_payment_label(expense_attributes: dict) -> str:
    method = (expense_attributes.get("payment_method") or "").lower()
    card = expense_attributes.get("card") or {}
    last4 = card.get("last4", "")
    acct = _get_payment_account(expense_attributes)
    name = _PAYMENT_ACCOUNT_NAME.get(acct, acct)
    if last4:
        return f"{name} (****{last4})"
    return name

def _get_account(expense_attributes: dict) -> str:
    cat = expense_attributes.get("category") or {}
    if isinstance(cat, dict):
        cid = str(cat.get("id") or "")
        cname = (cat.get("name") or "").strip().lower()
    elif cat:
        cid = str(cat)
        cname = str(cat).strip().lower()
    else:
        cid, cname = "", ""
    # 1) Buscar por ID string (ej. "restaurants")
    if cid and cid in _CATEGORY_ACCOUNT:
        return _CATEGORY_ACCOUNT[cid]
    # 2) Buscar por nombre (case-insensitive)
    if cname and cname in _CATEGORY_NAME_ACCOUNT:
        return _CATEGORY_NAME_ACCOUNT[cname]
    return _DEFAULT_ACCOUNT

def _get_category_name(expense_attributes: dict) -> str:
    cat = expense_attributes.get("category") or {}
    if isinstance(cat, dict):
        return cat.get("name") or str(cat.get("id") or "") or "—"
    elif cat:
        return str(cat)
    return expense_attributes.get("category_name") or "—"


# ── Nombre fiscal: extracción desde adjuntos ────────────────────────────────
_BUYER_KEYWORDS = ["km zero", "agua km", "kmzero", "km0", "km zero water"]

_CORP_RE = re.compile(
    r'([\w\u00C0-\u00FF][\w\u00C0-\u00FF\s\.\-&,]{2,50}?'
    r'(?:S\.?L\.?U?\.?|S\.?A\.?U?\.?|S\.?C\.?P?\.?|S\.?\s?COOP\.?|C\.?B\.?'
    r'|SOCIEDAD\s+LIMITADA|SOCIEDAD\s+AN[OÓ]NIMA))',
    re.IGNORECASE
)

def _load_fiscal_cache() -> dict:
    # Primero intentar desde GitHub (persistente en cloud)
    gh_data = _gh_load_fiscal()
    if gh_data:
        # Guardar local para tener copia
        try:
            tmp = FISCAL_CACHE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(gh_data, f, indent=2, ensure_ascii=False)
            tmp.replace(FISCAL_CACHE_FILE)
        except Exception:
            pass
        return gh_data
    # Fallback: fichero local
    if FISCAL_CACHE_FILE.exists():
        try:
            with open(FISCAL_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_fiscal_cache(cache: dict):
    # Guardar local
    tmp = FISCAL_CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    tmp.replace(FISCAL_CACHE_FILE)
    # Persistir en GitHub
    _gh_save_fiscal(cache)

def _extract_text_from_pdf(path: Path) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages[:3])
    except Exception:
        return ""

def _ocr_image(path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(path)
        return pytesseract.image_to_string(img, lang="spa")
    except Exception:
        return ""

def _extract_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return _extract_text_from_pdf(path)
    return _ocr_image(path)

def _parse_fiscal_name(text: str, known_tin: str) -> str | None:
    if not text:
        return None
    clean_tin = re.sub(r'[\s\-\.]', '', known_tin).upper()
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    # Estrategia 1: buscar CIF conocido y extraer nombre cerca
    for i, line in enumerate(lines):
        if clean_tin in re.sub(r'[\s\-\.]', '', line).upper():
            window = lines[max(0, i - 5):i + 3]
            for wl in window:
                for m in _CORP_RE.findall(wl):
                    name = re.sub(r'\s+', ' ', m).strip()
                    name = re.sub(r'^(?:Fdo\.?|Firmado\.?)\s*', '', name).strip()
                    if len(name) > 5 and not any(kw in name.lower() for kw in _BUYER_KEYWORDS):
                        return name

    # Estrategia 2: primer nombre corporativo que no sea el comprador
    for m in _CORP_RE.findall(text):
        name = re.sub(r'\s+', ' ', m).strip()
        name = re.sub(r'^(?:Fdo\.?|Firmado\.?)\s*', '', name).strip()
        if len(name) > 5 and not any(kw in name.lower() for kw in _BUYER_KEYWORDS):
            return name
    return None

def _lookup_einforma(tin: str) -> tuple[str | None, str | None]:
    """Busca razón social y ciudad en einforma.com por CIF/NIF con backoff exponencial.
    Devuelve (razón_social, ciudad) o (None, None)."""
    import html as _html
    clean = re.sub(r'[\s\-\.]', '', tin).upper()
    if len(clean) < 5:
        return None, None
    url = f"https://www.einforma.com/servlet/app/portal/ENTP/prod/ETIQUETA_EMPRESA/nif/{clean}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    delay = 1.0
    for attempt in range(4):
        try:
            r = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
            if r.status_code == 200 and len(r.text) > 500:
                text = _html.unescape(r.text)
                # Razón social
                name = None
                m = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.DOTALL)
                if m:
                    n = re.sub(r'\s+', ' ', m.group(1)).strip()
                    if len(n) > 3 and not any(kw in n.lower() for kw in _BUYER_KEYWORDS):
                        name = n
                # Ciudad: patrón "Localidad: XXXXX CIUDAD"
                city = None
                mc = re.search(r'Localidad:\s*(?:</?\w+[^>]*>\s*)*(\d{5})\s+([^(<\n]+)', text)
                if mc:
                    city = mc.group(2).strip()
                return name, city
            elif r.status_code == 429:
                time.sleep(delay)
                delay *= 2
                continue
            else:
                return None, None
        except Exception:
            time.sleep(delay)
            delay *= 2
    return None, None

def resolve_fiscal_names(expenses: list) -> dict:
    cache = _load_fiscal_cache()
    city_cache = _load_city_cache()
    updated = False
    city_updated = False
    seen_tins: set[str] = set()

    for expense in expenses:
        a = expense.get("attributes", expense)
        tin = (a.get("merchant_tin") or "").strip()
        if not tin:
            continue
        clean_tin = re.sub(r'[\s\-\.]', '', tin).upper()
        if clean_tin in seen_tins or len(clean_tin) < 5:
            continue
        seen_tins.add(clean_tin)

        need_name = clean_tin not in cache
        need_city = clean_tin not in city_cache

        if not need_name and not need_city:
            continue

        # 1) Intentar extraer nombre fiscal de factura adjunta (PDF/OCR)
        if need_name:
            files = a.get("files") or []
            if files:
                exp_id = str(expense.get("id") or a.get("id"))
                first_file = files[0] if isinstance(files[0], dict) else {}
                attach_path = download_attachment(first_file, exp_id)
                if attach_path and attach_path.exists():
                    text = _extract_text(attach_path)
                    fiscal = _parse_fiscal_name(text, tin)
                    if fiscal:
                        cache[clean_tin] = fiscal
                        updated = True
                        need_name = False

        # 2) Buscar en einforma.com (nombre fiscal + ciudad)
        if need_name or need_city:
            fiscal, city = _lookup_einforma(tin)
            if fiscal and need_name:
                cache[clean_tin] = fiscal
                updated = True
            if city and need_city:
                city_cache[clean_tin] = city
                city_updated = True

    if updated:
        _save_fiscal_cache(cache)
    if city_updated:
        _save_city_cache(city_cache)
    return cache

def _build_merchant_index(expenses: list, cache: dict) -> dict:
    """Construye un índice merchant_name → fiscal_name para gastos con CIFs válidos."""
    index: dict[str, str] = {}
    for e in expenses:
        a = e.get("attributes", e)
        tin = (a.get("merchant_tin") or "").strip()
        merchant = (a.get("merchant_name") or "").strip().upper()
        if not tin or not merchant:
            continue
        clean_tin = re.sub(r'[\s\-\.]', '', tin).upper()
        fiscal = cache.get(clean_tin)
        if fiscal and merchant not in index:
            index[merchant] = fiscal
    return index

def get_fiscal_name(tin: str, fallback: str, cache: dict,
                    merchant_index: dict | None = None) -> str:
    # 1) Buscar por CIF
    if tin:
        clean = re.sub(r'[\s\-\.]', '', tin).upper()
        if clean in cache:
            return cache[clean]
    # 2) Buscar por nombre comercial (para CIFs inválidos)
    if merchant_index and fallback:
        fiscal = merchant_index.get(fallback.strip().upper())
        if fiscal:
            return fiscal
    return fallback


# ── Caché de ciudades (CIF → ciudad) ────────────────────────────────────────
def _load_city_cache() -> dict:
    if CITY_CACHE_FILE.exists():
        try:
            with open(CITY_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_city_cache(cache: dict):
    tmp = CITY_CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    tmp.replace(CITY_CACHE_FILE)

def get_city(tin: str, city_cache: dict, merchant_name: str = "",
             merchant_city_idx: dict | None = None) -> str:
    if tin:
        clean = re.sub(r'[\s\-\.]', '', tin).upper()
        if clean in city_cache:
            return city_cache[clean]
    if merchant_city_idx and merchant_name:
        c = merchant_city_idx.get(merchant_name.strip().upper())
        if c:
            return c
    return ""

def _build_city_index(expenses: list, city_cache: dict) -> dict:
    index: dict[str, str] = {}
    for e in expenses:
        a = e.get("attributes", e)
        tin = (a.get("merchant_tin") or "").strip()
        merchant = (a.get("merchant_name") or "").strip().upper()
        if not tin or not merchant:
            continue
        clean = re.sub(r'[\s\-\.]', '', tin).upper()
        city = city_cache.get(clean)
        if city and merchant not in index:
            index[merchant] = city
    return index


# ── Formato de tag empleado ──────────────────────────────────────────────────
def _format_employee_tag(full_name: str) -> str:
    """Formatea 'Germán Gutierrez Brun' → 'germangutierrez' (nombre + primer apellido, sin acentos)."""
    import unicodedata
    # Quitar acentos
    normalized = unicodedata.normalize("NFD", full_name)
    ascii_name = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    parts = ascii_name.strip().split()
    if len(parts) >= 2:
        return (parts[0] + parts[1]).lower()
    elif parts:
        return parts[0].lower()
    return ""


# ── Helpers ───────────────────────────────────────────────────────────────────
def _shift_march_to_april(date_str: str) -> str:
    """Si la fecha es de marzo, la mueve a abril (mismo día)."""
    try:
        d = _date.fromisoformat(date_str[:10])
        if d.month == 3:
            return d.replace(month=4).isoformat()
    except Exception:
        pass
    return date_str

def to_unix(date_str: str) -> int:
    try:
        shifted = _shift_march_to_april(date_str)
        d = _date.fromisoformat(shifted[:10])
        return int(datetime(d.year, d.month, d.day).timestamp())
    except Exception:
        return int(datetime.utcnow().timestamp())

def get_local_attachment(expense_id: str) -> Path | None:
    for ext in (".pdf", ".jpg", ".png", ".jpeg"):
        p = ATTACH_DIR / f"expense_{expense_id}{ext}"
        if p.exists():
            return p
    return None

def download_attachment(file_info: dict, expense_id: str) -> Path | None:
    existing = get_local_attachment(expense_id)
    if existing:
        return existing
    url = (file_info.get("url") or file_info.get("download_url") or
           (file_info.get("attributes") or {}).get("url"))
    if not url or not _is_allowed_url(url):
        return None
    try:
        is_factorial = (urlparse(url).hostname or "") == "factorialhr.com"
        auth_h = _f_headers() if is_factorial else {}
        # Verificar tamaño antes de descargar completo
        head = requests.head(url, headers=auth_h, timeout=10, verify=True, allow_redirects=True)
        size = int(head.headers.get("Content-Length", 0))
        if size > MAX_ATTACH_BYTES:
            return None
        r = requests.get(url, headers=auth_h, timeout=30, verify=True,
                         stream=True, allow_redirects=True)
        if r.status_code != 200:
            return None
        ct  = r.headers.get("Content-Type", "")
        ext = ".pdf" if "pdf" in ct else (".png" if "png" in ct else ".jpg")
        chunks, total = [], 0
        for chunk in r.iter_content(65536):
            total += len(chunk)
            if total > MAX_ATTACH_BYTES:
                return None
            chunks.append(chunk)
        safe_id = "".join(c for c in str(expense_id) if c.isalnum() or c in "-_")
        path = ATTACH_DIR / f"expense_{safe_id}{ext}"
        path.write_bytes(b"".join(chunks))
        return path
    except Exception:
        return None

def cleanup_attachments():
    """Elimina adjuntos temporales del disco tras sincronizar."""
    try:
        for f in ATTACH_DIR.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
    except Exception:
        pass

def get_or_create_contact(name: str, tin: str) -> str | None:
    if not name:
        return None
    if tin:
        r = requests.get(f"{HOLDED_BASE}/contacts/v1/contacts",
                         headers=_h_headers(), params={"vatNumber": tin},
                         verify=True, timeout=30)
        if r.status_code == 200:
            try:
                items = r.json() if r.text.strip() else []
            except Exception:
                items = []
            items = items if isinstance(items, list) else items.get("contacts", items.get("data", []))
            if items:
                return items[0].get("code") or items[0].get("id")
        else:
            return None
    r = requests.post(f"{HOLDED_BASE}/contacts/v1/contacts",
                      json={"name": name, "vatNumber": tin or "",
                            "type": ["supplier"], "isPerson": False},
                      headers=_h_headers(), verify=True, timeout=30)
    if r.status_code in (200, 201):
        try:
            c = r.json() if r.text.strip() else {}
            return c.get("code") or c.get("id")
        except Exception:
            return None
    return None

def push_to_holded(expense: dict, attach_path: Path | None,
                   employee_name: str = "") -> tuple[bool, str]:
    a            = expense.get("attributes", expense)
    amount_eur   = (a.get("amount") or 0) / 100.0
    description      = a.get("description") or a.get("user_merchant") or a.get("merchant_name") or "Gasto"
    expense_acc_num  = _get_account(a)
    expense_acc_name = _ACCOUNT_NAME.get(expense_acc_num, "Otros servicios")
    merchant     = a.get("merchant_name") or a.get("user_merchant") or ""
    merchant_tin = a.get("merchant_tin") or ""
    effective_on = a.get("effective_on") or ""
    currency     = a.get("currency") or "EUR"
    taxes        = a.get("taxes") or []
    is_invoice   = (a.get("document_type") or "").lower() == "invoice"
    expense_acc     = _get_account(a)
    expense_acc_id  = _ACCOUNT_HOLDED_ID.get(expense_acc, _DEFAULT_ACCOUNT_HOLDED_ID)

    if is_invoice and taxes:
        # FACTURA: IVA desgravable → separar base e IVA, registrar en cta. 472
        rate = taxes[0].get("percentage") or taxes[0].get("rate") or 0
        vat_rate = rate if rate > 1 else round(rate * 100)
        base = round(amount_eur / (1 + vat_rate / 100), 4) if vat_rate else amount_eur
        item = {"name": description, "units": 1, "subtotal": base, "tax": vat_rate,
                "account": expense_acc_id}
    else:
        # TICKET/RECIBO: IVA NO desgravable → importe íntegro como gasto
        base = amount_eur
        item = {"name": description, "units": 1, "subtotal": base, "tax": 0,
                "account": expense_acc_id}

    # Tags: cuenta contable + ciudad (por CIF) + empleado
    tags = []
    tags.append(f"{expense_acc_num} {expense_acc_name}")
    cc = _load_city_cache()
    city = get_city(merchant_tin, cc, merchant,
                    _build_city_index([expense], cc))
    if city:
        tags.append(city.lower())
    if employee_name:
        tags.append(_format_employee_tag(employee_name))

    payload = {
        "date":     to_unix(effective_on) if effective_on else int(datetime.utcnow().timestamp()),
        "notes":    description,
        "currency": currency,
        "account":  _get_payment_account(a),
        "items":    [item],
        "tags":     tags
    }
    # Usar nombre fiscal si lo tenemos, sino el comercial
    fc = _load_fiscal_cache()
    fiscal_name = get_fiscal_name(merchant_tin, merchant, fc,
                                  _build_merchant_index([expense], fc))
    contact_code = get_or_create_contact(fiscal_name, merchant_tin)
    if contact_code:
        payload["contactCode"] = contact_code
    elif fiscal_name:
        payload["contactName"] = fiscal_name

    r = requests.post(f"{HOLDED_BASE}/invoicing/v1/documents/purchase",
                      json=payload, headers=_h_headers(), timeout=30)
    if r.status_code not in (200, 201):
        # No exponer body de respuesta al usuario
        return False, f"Error en Holded (HTTP {r.status_code})"
    try:
        holded_id = r.json().get("id") or r.json().get("docId")
    except Exception:
        holded_id = None
    if attach_path and attach_path.exists() and holded_id:
        with open(attach_path, "rb") as f:
            requests.post(
                f"{HOLDED_BASE}/invoicing/v1/documents/purchase/{holded_id}/attach",
                headers={"key": HOLDED_API_KEY or ""},
                files={"file": (attach_path.name, f, "application/octet-stream")},
                timeout=60
            )

    # Registrar el pago en Holded (contra la cuenta de tesorería)
    payment_acct = _get_payment_account(a)
    treasury_id = _TREASURY_ID.get(payment_acct)
    if holded_id and treasury_id:
        pay_date = to_unix(effective_on) if effective_on else int(datetime.utcnow().timestamp())
        pay_payload = {"amount": amount_eur, "date": pay_date, "treasury": treasury_id}
        rp = requests.post(
            f"{HOLDED_BASE}/invoicing/v1/documents/purchase/{holded_id}/pay",
            json=pay_payload, headers=_h_headers(), timeout=15
        )
        if rp.status_code != 200 or not rp.json().get("status"):
            _audit.warning(f"PAYMENT | FAIL | doc={holded_id} | HTTP {rp.status_code}")

    return True, "OK"


# ════════════════════════════════════════════════════════════
# UI
# ════════════════════════════════════════════════════════════
st.title("💸 Gastos Factorial → Holded")

if not FACTORIAL_API_KEY or not HOLDED_API_KEY:
    st.error("⚙️ Configura FACTORIAL_API_KEY y HOLDED_API_KEY en los Secrets")
    st.stop()

with st.spinner("Cargando gastos y empleados..."):
    all_expenses = fetch_all_expenses()
    employees    = fetch_employees()

synced_ids      = get_synced_ids()
fiscal_cache    = _load_fiscal_cache()

# Buscar automáticamente nombres fiscales por CIF para proveedores no cacheados
_tins_missing = set()
for _e in all_expenses:
    _a = _e.get("attributes", _e)
    _tin = re.sub(r'[\s\-\.]', '', (_a.get("merchant_tin") or "")).upper()
    if _tin and len(_tin) >= 5 and _tin not in fiscal_cache:
        _tins_missing.add(_tin)

if _tins_missing:
    with st.spinner(f"Buscando nombres fiscales para {len(_tins_missing)} proveedor(es)..."):
        fiscal_cache.update(resolve_fiscal_names(all_expenses))

merchant_idx    = _build_merchant_index(all_expenses, fiscal_cache)
employee_names  = sorted({n for n in employees.values() if n})

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔍 Filtros")
    status_opts  = ["approved", "paid", "pending", "draft", "rejected", "reversed"]
    sel_statuses = st.multiselect("Estado", status_opts, default=["approved", "paid"])
    sel_employees = st.multiselect("Empleado", employee_names,
                                   placeholder="Todos los empleados")
    date_from = st.date_input("Desde", value=None)
    date_to   = st.date_input("Hasta", value=None)
    search    = st.text_input("🔎 Buscar", placeholder="Concepto o proveedor...")
    st.divider()
    if st.button("🔄 Refrescar ahora", use_container_width=True):
        st.cache_data.clear()
        _mark_fetched()
        st.rerun()
    if LAST_FETCH_FILE.exists():
        try:
            last_ts = datetime.fromisoformat(LAST_FETCH_FILE.read_text().strip())
            st.caption(f"Última actualización:\n{last_ts.strftime('%d/%m/%Y %H:%M')} UTC")
        except Exception:
            pass
    st.divider()
    n_cached = len(fiscal_cache)
    st.caption(f"Nombres fiscales en caché: {n_cached}")
    if st.button("🔍 Resolver nombres fiscales", use_container_width=True,
                 help="Lee las facturas adjuntas para extraer el nombre fiscal real del proveedor"):
        with st.spinner("Leyendo facturas para extraer nombres fiscales..."):
            fiscal_cache.update(resolve_fiscal_names(all_expenses))
        st.rerun()
    st.divider()
    if st.button("🔒 Cerrar sesión", use_container_width=True):
        st.session_state["_auth_ok"] = False
        st.session_state.pop("_auth_ts", None)
        _log_op("LOGOUT")
        st.rerun()

# ── DataFrame ─────────────────────────────────────────────────────────────────
rows = []
for e in all_expenses:
    a      = e.get("attributes", e)
    exp_id = str(e.get("id") or a.get("id"))
    emp_id = str(a.get("employee_id") or "")
    raw_date = (a.get("effective_on") or "")[:10]
    # Excluir gastos de febrero
    try:
        if _date.fromisoformat(raw_date).month == 2:
            continue
    except Exception:
        pass
    taxes        = a.get("taxes") or []
    is_invoice   = (a.get("document_type") or "").lower() == "invoice"
    # IVA solo desgravable si document_type es "invoice" (factura);
    # con "receipt" (ticket) el IVA va incluido en el gasto, no se desgrava
    vat_rate     = 0
    if taxes and is_invoice:
        rate     = taxes[0].get("percentage") or taxes[0].get("rate") or 0
        vat_rate = rate if rate > 1 else round(rate * 100)
    display_date = _shift_march_to_april(raw_date)
    rows.append({
        "ID":          exp_id,
        "Empleado":    employees.get(emp_id, emp_id),
        "Fecha":       display_date,
        "Concepto":    (
                           (a.get("description") or a.get("merchant_name") or "—")[:50]
                           + (" · " + _get_category_name(a) if _get_category_name(a) != "—" else "")
                       ),
        "Proveedor":   get_fiscal_name(
                           a.get("merchant_tin") or "",
                           a.get("merchant_name") or "—",
                           fiscal_cache, merchant_idx)[:55],
        "CIF":         (a.get("merchant_tin") or "—"),
        "Importe":     round((a.get("amount") or 0) / 100.0, 2),
        "Moneda":      a.get("currency") or "EUR",
        "Tipo doc.":   "Factura" if is_invoice else "Ticket",
        "IVA %":       vat_rate,
        "IVA desgr.":  "Sí" if (vat_rate and is_invoice) else "No",
        "Categoría":   _get_category_name(a),
        "Cta. gasto":  _get_account(a),
        "Cta. nombre": _ACCOUNT_NAME.get(_get_account(a), "Otros servicios"),
        "Cta. pago":   _get_payment_account(a),
        "Medio pago":  _get_payment_label(a),
        "Estado":      a.get("status") or "—",
        "Adjunto":     bool(a.get("files")),
        "En Holded":   exp_id in synced_ids,
    })

df_full = pd.DataFrame(rows) if rows else pd.DataFrame()

df = df_full.copy()
if sel_statuses:
    df = df[df["Estado"].isin(sel_statuses)]
if sel_employees:
    df = df[df["Empleado"].isin(sel_employees)]
if date_from:
    df = df[df["Fecha"] >= str(date_from)]
if date_to:
    df = df[df["Fecha"] <= str(date_to)]
if search:
    # regex=False previene ReDoS con inputs de usuario
    mask = (df["Concepto"].str.contains(search, case=False, na=False, regex=False) |
            df["Proveedor"].str.contains(search, case=False, na=False, regex=False))
    df = df[mask]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Gastos (filtrado)",  len(df))
c2.metric("Pendientes Holded",  int((~df["En Holded"]).sum()) if not df.empty else 0)
c3.metric("Ya en Holded",       int(df["En Holded"].sum())    if not df.empty else 0)
c4.metric("Con factura",        int((df["Tipo doc."] == "Factura").sum()) if not df.empty else 0)
st.divider()

if df.empty:
    st.info("No hay gastos con los filtros actuales.")
    st.stop()

display_cols = ["ID", "Empleado", "Fecha", "Concepto", "Proveedor", "CIF",
                "Importe", "Moneda", "Tipo doc.", "IVA %", "IVA desgr.", "Categoría",
                "Cta. gasto", "Cta. nombre", "Cta. pago", "Medio pago",
                "Estado", "Adjunto", "En Holded"]
df_display = df[display_cols].copy()
df_display.insert(0, "✓", False)

# Gastos ya en Holded no se pueden seleccionar
df_not_synced = df_display[df_display["En Holded"] == False].copy()
df_synced     = df_display[df_display["En Holded"] == True].copy()

col_a, col_b = st.columns([1, 1])
if col_a.button("☑️ Seleccionar todo (pendientes)"):
    st.session_state["_sel_all"]  = True
    st.session_state["_sel_none"] = False
if col_b.button("🔲 Limpiar selección"):
    st.session_state["_sel_none"] = True
    st.session_state["_sel_all"]  = False

if st.session_state.get("_sel_all"):
    df_not_synced["✓"] = True
elif st.session_state.get("_sel_none"):
    df_not_synced["✓"] = False

_col_cfg = {
    "✓":          st.column_config.CheckboxColumn("✓", width="small"),
    "Importe":    st.column_config.NumberColumn("Importe", format="%.2f €"),
    "IVA %":      st.column_config.NumberColumn("IVA %", format="%d%%"),
    "Tipo doc.":  st.column_config.TextColumn("Tipo doc.", width="small"),
    "Cta. gasto": st.column_config.TextColumn("Cta. gasto", width="small"),
    "Adjunto":    st.column_config.CheckboxColumn("📎", width="small"),
    "En Holded":  st.column_config.CheckboxColumn("✅ Holded", width="small"),
}

if not df_not_synced.empty:
    st.subheader(f"Pendientes de sincronizar ({len(df_not_synced)})")
    edited = st.data_editor(
        df_not_synced,
        use_container_width=True,
        hide_index=True,
        disabled=display_cols,
        column_config=_col_cfg,
        key="expense_table"
    )
    selected_ids = list(edited[edited["✓"] == True]["ID"])
else:
    selected_ids = []

if not df_synced.empty:
    st.subheader(f"Ya en Holded ({len(df_synced)})")
    st.dataframe(
        df_synced.drop(columns=["✓"]),
        use_container_width=True,
        hide_index=True,
        column_config=_col_cfg,
    )
st.caption(f"{len(selected_ids)} seleccionados")

# ── Descargar facturas adjuntas ───────────────────────────────────────────────
expenses_with_files = [
    e for e in all_expenses
    if (e.get("attributes", e).get("files") or [])
    and str(e.get("id") or (e.get("attributes", e)).get("id")) in set(df["ID"].astype(str))
]

if expenses_with_files:
    st.divider()
    with st.expander(f"📎 Descargar facturas adjuntas ({len(expenses_with_files)})"):
        for e in expenses_with_files:
            a      = e.get("attributes", e)
            exp_id = str(e.get("id") or a.get("id"))
            desc   = (a.get("description") or a.get("merchant_name") or "Gasto")[:50]
            date   = _shift_march_to_april((a.get("effective_on") or "")[:10])
            amount = round((a.get("amount") or 0) / 100.0, 2)
            c1, c2 = st.columns([5, 1])
            c1.write(f"**{date}** · {desc} · {amount} €")
            dl_key = f"_dl_{exp_id}"
            if dl_key in st.session_state:
                data, fname = st.session_state[dl_key]
                c2.download_button("💾 Guardar", data=data, file_name=fname,
                                   key=f"save_{exp_id}", use_container_width=True)
            else:
                if c2.button("⬇️ Cargar", key=f"fetch_{exp_id}", use_container_width=True):
                    files = a.get("files") or []
                    first = files[0] if isinstance(files[0], dict) else {}
                    path = download_attachment(first, exp_id)
                    if path and path.exists():
                        st.session_state[dl_key] = (path.read_bytes(), path.name)
                        st.rerun()
                    else:
                        st.error("No se pudo descargar la factura")

# ── Copiar cuenta contable al portapapeles ───────────────────────────────────
if not df_synced.empty:
    st.divider()
    st.subheader("Copiar cuenta contable")
    st.caption("Click en una cuenta para copiarla al portapapeles y pegarla en Holded")
    unique_accounts = df_synced[["Cta. gasto", "Cta. nombre"]].drop_duplicates().values.tolist()
    cols = st.columns(min(len(unique_accounts), 4))
    for i, (num, name) in enumerate(unique_accounts):
        with cols[i % len(cols)]:
            st.code(num, language=None)
            st.caption(name)

# ── Exportar CSV para Holded ──────────────────────────────────────────────────
st.divider()
st.subheader("📥 Exportar para importar en Holded")

_export_ids = selected_ids if selected_ids else list(df["ID"].astype(str))
_export_expenses = [e for e in all_expenses
                    if str(e.get("id") or (e.get("attributes", e)).get("id")) in set(_export_ids)]

if _export_expenses:
    city_cache_exp = _load_city_cache()
    city_idx_exp   = _build_city_index(_export_expenses, city_cache_exp)
    _export_rows = []
    for e in _export_expenses:
        a      = e.get("attributes", e)
        tin    = (a.get("merchant_tin") or "").strip()
        raw_date = (a.get("effective_on") or "")[:10]
        fecha  = _shift_march_to_april(raw_date)
        try:
            fecha_fmt = _date.fromisoformat(fecha).strftime("%d/%m/%Y")
        except Exception:
            fecha_fmt = fecha
        fiscal = get_fiscal_name(tin, a.get("merchant_name") or "", fiscal_cache,
                                 _build_merchant_index([e], fiscal_cache))
        city   = get_city(tin, city_cache_exp, a.get("merchant_name") or "", city_idx_exp)
        desc   = (a.get("description") or a.get("merchant_name") or "")
        _export_rows.append({
            "Num factura":              str(e.get("id") or a.get("id")),
            "Fecha":                    fecha_fmt,
            "Fecha de vencimiento":     fecha_fmt,
            "Fecha deducción":          fecha_fmt,
            "Descripción":              desc,
            "Nombre del contacto":      fiscal,
            "NIF":                      tin,
            "Dirección":                "",
            "Población":                city,
            "Código postal":            "",
            "Provincia":                "",
            "País":                     "España",
            "Concepto":                 _get_category_name(a),
        })
    df_export = pd.DataFrame(_export_rows)
    csv_bytes  = df_export.to_csv(index=False, sep=";", encoding="utf-8-sig").encode("utf-8-sig")
    label = f"Seleccionados ({len(_export_ids)})" if selected_ids else f"Todos ({len(_export_ids)})"
    st.download_button(
        label=f"⬇️ Descargar CSV para Holded — {label}",
        data=csv_bytes,
        file_name="holded_import.csv",
        mime="text/csv",
        use_container_width=True,
    )
    st.caption("Separador: punto y coma · Codificación: UTF-8 · Importar en Holded → Compras → Importar")

st.divider()
col_dry, col_btn = st.columns([1, 4])
with col_dry:
    dry_run = st.toggle("🔮 Modo prueba", value=False, help="Simula sin enviar nada a Holded")
with col_btn:
    sync_btn = st.button(
        f"{'🔮 Simular' if dry_run else '🚀 Sincronizar'} ({len(selected_ids)} seleccionados)",
        type="primary",
        disabled=len(selected_ids) == 0,
        use_container_width=True
    )

if sync_btn and selected_ids:
    to_sync  = [e for e in all_expenses
                if str(e.get("id") or (e.get("attributes") or {}).get("id")) in selected_ids]
    progress = st.progress(0)
    ok_count = fail_count = skip_count = 0

    for idx, expense in enumerate(to_sync):
        a      = expense.get("attributes", expense)
        exp_id = str(expense.get("id") or a.get("id"))
        desc   = (a.get("description") or a.get("merchant_name") or "Gasto")[:40]
        amount = (a.get("amount") or 0) / 100.0

        with st.spinner(f"{desc} — {amount:.2f} €"):
            if dry_run:
                already = "ya en Holded" if exp_id in synced_ids else "nuevo"
                st.success(f"🔮 [SIMULADO] {desc} — {amount:.2f} € ({already})")
                ok_count += 1
                _log_op("DRY_RUN", exp_id, desc, amount)
            elif exp_id in synced_ids:
                st.info(f"⏭️ Ya sincronizado: {desc}")
                skip_count += 1
            else:
                files = a.get("files") or []
                attach_path = None
                if files:
                    first = files[0] if isinstance(files[0], dict) else {}
                    attach_path = download_attachment(first, exp_id)
                emp_id = str(a.get("employee_id") or "")
                emp_name = employees.get(emp_id, "")
                ok, msg = push_to_holded(expense, attach_path, emp_name)
                if ok:
                    synced_ids.add(exp_id)
                    ok_count += 1
                    st.success(f"✅ {desc} — {amount:.2f} €")
                    _log_op("SYNCED_OK", exp_id, desc, amount)
                else:
                    fail_count += 1
                    st.error(f"❌ {desc}: {msg}")
                    _log_op("SYNC_FAIL", exp_id, desc, amount)

        progress.progress((idx + 1) / len(to_sync))
        time.sleep(0.3)

    if not dry_run and ok_count:
        data = load_synced()
        data["synced_ids"] = list(synced_ids)
        save_synced(data)
        cleanup_attachments()   # Eliminar facturas temporales del disco
        st.cache_data.clear()
        st.session_state["_sel_all"]  = False
        st.session_state["_sel_none"] = True

    st.divider()
    r1, r2, r3 = st.columns(3)
    r1.metric("✅ Enviados",    ok_count)
    r2.metric("⏭️ Ya existían", skip_count)
    if fail_count:
        r3.metric("❌ Fallidos", fail_count)
    if ok_count and not dry_run:
        st.balloons()

st.markdown("---")
st.caption("Herramienta interna · v2.2 · Acceso restringido")
