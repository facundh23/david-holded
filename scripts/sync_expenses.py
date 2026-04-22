"""
Script principal: sincroniza gastos aprobados de Factorial → Holded.

Uso:
  python scripts/sync_expenses.py              # sincronización real
  python scripts/sync_expenses.py --dry-run    # solo muestra qué haría
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import requests
from datetime import datetime, date
from urllib.parse import urlparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

FACTORIAL_API_KEY     = os.getenv("FACTORIAL_API_KEY")
HOLDED_API_KEY        = os.getenv("HOLDED_API_KEY")
FACTORIAL_EMPLOYEE_ID = os.getenv("FACTORIAL_EMPLOYEE_ID")
FACTORIAL_BASE        = "https://api.factorialhr.com/api/2026-01-01/resources"
HOLDED_BASE           = "https://api.holded.com/api"
DRY_RUN               = "--dry-run" in sys.argv

DATA_DIR    = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SYNCED_FILE = DATA_DIR / "synced_expenses.json"
ATTACH_DIR  = DATA_DIR / "attachments"
ATTACH_DIR.mkdir(exist_ok=True)

def _f_headers() -> dict:
    return {"x-api-key": FACTORIAL_API_KEY or "", "Accept": "application/json"}

def _h_headers() -> dict:
    return {"key": HOLDED_API_KEY or "", "Accept": "application/json"}


def load_synced():
    if SYNCED_FILE.exists():
        with open(SYNCED_FILE) as f:
            return set(str(i) for i in json.load(f).get("synced_ids", []))
    return set()


def save_synced(ids: set):
    data = {"synced_ids": list(ids),
            "last_sync": datetime.utcnow().isoformat() + "Z"}
    # Escritura atómica: .tmp → rename, evita corrupción si el proceso se interrumpe
    tmp = SYNCED_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(SYNCED_FILE)


def fetch_expenses():
    params = {
        "include_attachments": "true",
        "include_manual_drafts": "false",
        "include_grouped": "false",
        "status[]": "approved",
        "per_page": 100
    }
    if FACTORIAL_EMPLOYEE_ID:
        params["employee_ids[]"] = FACTORIAL_EMPLOYEE_ID

    all_items, page = [], 1
    while True:
        params["page"] = page
        r = requests.get(f"{FACTORIAL_BASE}/expenses/expenses",
                         headers=_f_headers(), params=params, timeout=30)
        if r.status_code != 200:
            print(f"  ❌ Error Factorial: HTTP {r.status_code}")
            break
        items = r.json().get("data", [])
        all_items.extend(items)
        if len(items) < 100:
            break
        page += 1
        time.sleep(0.3)
    return all_items


_ALLOWED_HOSTS = frozenset({"factorialhr.com", "cdn.factorialhr.com", "storage.googleapis.com"})
_S3_RE = re.compile(r'^([\w-]+\.)?s3([.\-][\w-]+)*\.amazonaws\.com$', re.IGNORECASE)
MAX_ATTACH_BYTES = 10 * 1024 * 1024

def _is_allowed_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        host = (parsed.hostname or "").lower().strip(".")
        if not host or len(host) > 253:
            return False
        if host in _ALLOWED_HOSTS:
            return True
        if host.endswith(".amazonaws.com") and _S3_RE.match(host):
            return True
        return False
    except Exception:
        return False

def download_file(file_info: dict, expense_id) -> Path | None:
    url = (file_info.get("url") or file_info.get("download_url") or
           (file_info.get("attributes") or {}).get("url"))
    if not url or not _is_allowed_url(url):
        return None
    try:
        # Solo enviar credenciales si el hostname es exactamente factorialhr.com
        host = (urlparse(url).hostname or "").lower()
        auth_headers = _f_headers() if host == "factorialhr.com" else {}
        r = requests.get(url, headers=auth_headers, timeout=30, verify=True,
                         stream=True, allow_redirects=True)
        if r.status_code != 200:
            return None
        ct = r.headers.get("Content-Type", "")
        ext = ".pdf" if "pdf" in ct else ".jpg"
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
    except Exception as e:
        print(f"  ⚠️  No se pudo descargar adjunto: {type(e).__name__}")
    return None


def get_or_create_contact(merchant_name: str, merchant_tin: str) -> str | None:
    if not merchant_name:
        return None
    # Buscar por NIF primero para evitar duplicados
    if merchant_tin:
        r = requests.get(f"{HOLDED_BASE}/contacts/v1/contacts",
                         headers=_h_headers(), params={"vatNumber": merchant_tin},
                         verify=True, timeout=30)
        if r.status_code == 200:
            if not r.text.strip():
                contacts = []
            else:
                try:
                    contacts = r.json()
                except Exception:
                    contacts = []
            items = contacts if isinstance(contacts, list) else contacts.get("contacts", contacts.get("data", []))
            if items:
                return items[0].get("code") or items[0].get("id")
            # 200 sin resultados → contacto no existe, se puede crear
        else:
            # Error de API (5xx, 429, etc.) → no crear para evitar duplicados
            print(f"  ⚠️  Error buscando contacto en Holded (HTTP {r.status_code}), se omite creación")
            return None
    # Crear contacto (solo si la búsqueda devolvió 200 sin resultados, o no hay NIF)
    payload = {"name": merchant_name, "vatNumber": merchant_tin or "",
               "type": ["supplier"], "isPerson": False}
    r = requests.post(f"{HOLDED_BASE}/contacts/v1/contacts",
                      json=payload, headers=_h_headers(), verify=True, timeout=30)
    if r.status_code in (200, 201):
        if not r.text.strip():
            return None
        try:
            c = r.json()
            return c.get("code") or c.get("id")
        except Exception:
            return None
    return None


def to_unix(date_str: str) -> int:
    try:
        d = date.fromisoformat(date_str[:10])
        return int(datetime(d.year, d.month, d.day).timestamp())
    except Exception:
        return int(datetime.utcnow().timestamp())


def push_to_holded(expense: dict, attach_path: Path | None) -> tuple[bool, str]:
    a             = expense.get("attributes", expense)
    amount_eur    = (a.get("amount") or 0) / 100.0
    description   = a.get("description") or a.get("user_merchant") or a.get("merchant_name") or "Gasto"
    merchant      = a.get("merchant_name") or a.get("user_merchant") or ""
    merchant_tin  = a.get("merchant_tin") or ""
    effective_on  = a.get("effective_on") or ""
    currency      = a.get("currency") or "EUR"
    taxes         = a.get("taxes") or []
    is_invoice    = (a.get("document_type") or "").lower() == "invoice"

    if is_invoice and taxes:
        # FACTURA: IVA desgravable → separar base e IVA, registrar en cta. 472
        rate = taxes[0].get("percentage") or taxes[0].get("rate") or 0
        vat_rate = rate if rate > 1 else round(rate * 100)
        base_amount = round(amount_eur / (1 + vat_rate / 100), 4) if vat_rate else amount_eur
        item = {"name": description, "units": 1, "subtotal": base_amount, "tax": vat_rate,
                "account": "62900000", "taxAccount": "47200000"}
    else:
        # TICKET/RECIBO: IVA NO desgravable → importe íntegro como gasto
        base_amount = amount_eur
        item = {"name": description, "units": 1, "subtotal": base_amount, "tax": 0,
                "account": "62900000"}

    payload = {
        "date": to_unix(effective_on) if effective_on else int(datetime.utcnow().timestamp()),
        "notes": description,
        "currency": currency,
        "account": "57290016",
        "items": [item]
    }

    contact_code = get_or_create_contact(merchant, merchant_tin)
    if contact_code:
        payload["contactCode"] = contact_code
    elif merchant:
        payload["contactName"] = merchant

    r = requests.post(f"{HOLDED_BASE}/invoicing/v1/documents/purchase",
                      json=payload, headers=_h_headers(), timeout=30)
    if r.status_code not in (200, 201):
        return False, f"Error en Holded (HTTP {r.status_code})"

    holded_id = r.json().get("id") or r.json().get("docId")

    if attach_path and attach_path.exists() and holded_id:
        with open(attach_path, "rb") as f:
            r2 = requests.post(
                f"{HOLDED_BASE}/invoicing/v1/documents/purchase/{holded_id}/attach",
                headers={"key": HOLDED_API_KEY},
                files={"file": (attach_path.name, f, "application/octet-stream")},
                timeout=60
            )
            if r2.status_code not in (200, 201):
                print(f"  ⚠️  Adjunto no enviado: {r2.status_code}")

    return True, "OK"


def main():
    print("=" * 65)
    print("💸 SINCRONIZACIÓN: Factorial → Holded")
    print(f"   Modo: {'DRY-RUN (sin cambios en Holded)' if DRY_RUN else 'PRODUCCIÓN'}")
    print("=" * 65)

    if not FACTORIAL_API_KEY or not HOLDED_API_KEY:
        print("❌ Configura FACTORIAL_API_KEY y HOLDED_API_KEY en .env")
        sys.exit(1)

    synced = load_synced()
    print(f"\n📋 Ya sincronizados anteriormente: {len(synced)}")
    print("🔄 Obteniendo gastos aprobados de Factorial...")
    expenses = fetch_expenses()
    print(f"   ✅ {len(expenses)} gasto(s) aprobado(s) en Factorial")

    new_exp = [e for e in expenses
               if str(e.get("id") or (e.get("attributes") or {}).get("id")) not in synced]
    print(f"   🆕 {len(new_exp)} gasto(s) nuevos\n")

    if not new_exp:
        print("✅ Todo al día. No hay gastos nuevos.")
        return

    ok_count = fail_count = 0
    for expense in new_exp:
        a       = expense.get("attributes", expense)
        exp_id  = str(expense.get("id") or a.get("id"))
        desc    = (a.get("description") or a.get("merchant_name") or "Sin desc")[:50]
        amount  = (a.get("amount") or 0) / 100.0
        date_s  = (a.get("effective_on") or "")[:10]

        print(f"  📄 [{exp_id}] {desc} | {amount:.2f} EUR | {date_s}")

        if DRY_RUN:
            print(f"     🔮 [DRY-RUN] Se crearía en Holded")
            ok_count += 1
            continue

        # Descargar adjunto
        files        = a.get("files") or []
        attach_path  = None
        if files:
            first = files[0] if isinstance(files[0], dict) else {}
            attach_path = download_file(first, exp_id)
            if attach_path:
                print(f"     📥 Factura descargada: {attach_path.name}")

        ok, msg = push_to_holded(expense, attach_path)
        if ok:
            synced.add(exp_id)
            ok_count += 1
            print(f"     ✅ Enviado a Holded")
        else:
            fail_count += 1
            print(f"     ❌ Error: {msg}")

        time.sleep(0.5)

    if not DRY_RUN:
        save_synced(synced)

    print()
    print("=" * 65)
    print(f"✅ Sincronizados: {ok_count}  |  ❌ Fallidos: {fail_count}")
    if DRY_RUN:
        print("   (DRY-RUN: ningún cambio se realizó en Holded)")
    print("=" * 65)


if __name__ == "__main__":
    main()
