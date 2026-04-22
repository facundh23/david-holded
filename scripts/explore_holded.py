"""
Script de exploración: muestra compras/gastos existentes en Holded.
Ejecutar: python scripts/explore_holded.py
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("HOLDED_API_KEY")
BASE    = "https://api.holded.com/api"
HEADERS = {"key": API_KEY, "Accept": "application/json"}

print("=" * 70)
print("🔍 EXPLORANDO HOLDED")
print("=" * 70)

if not API_KEY:
    print("❌ HOLDED_API_KEY no configurada en .env")
    exit(1)

# 1. Documentos de compra existentes
print("\n1️⃣  Documentos de compra (purchases) en Holded:")
r = requests.get(f"{BASE}/invoicing/v1/documents/purchase", headers=HEADERS)
print(f"   Status: {r.status_code}")
if r.status_code == 200:
    docs = r.json()
    items = docs if isinstance(docs, list) else docs.get("items", docs.get("data", []))
    print(f"   ✅ {len(items)} documento(s)")
    if items:
        print("\n   📋 Estructura del primer documento:")
        print(json.dumps(items[0], indent=4, ensure_ascii=False)[:1500])
else:
    print(f"   ❌ {r.text[:400]}")

# 2. Contactos/proveedores
print("\n2️⃣  Proveedores (primeros 5):")
r2 = requests.get(f"{BASE}/contacts/v1/contacts", headers=HEADERS,
                  params={"type": "supplier", "limit": 5})
print(f"   Status: {r2.status_code}")
if r2.status_code == 200:
    contacts = r2.json()
    items2 = contacts if isinstance(contacts, list) else contacts.get("contacts", contacts.get("data", []))
    for c in items2[:5]:
        print(f"   - {c.get('name', '')}  |  NIF: {c.get('vatNumber', '')}  |  ID: {c.get('id', '')}")
else:
    print(f"   ❌ {r2.text[:300]}")

print("\n" + "=" * 70)
