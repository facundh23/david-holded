"""
Script de exploración: muestra empleados y estructura de gastos en Factorial.
Ejecutar: python scripts/explore_factorial.py
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY     = os.getenv("FACTORIAL_API_KEY")
EMPLOYEE_ID = os.getenv("FACTORIAL_EMPLOYEE_ID")
BASE        = "https://api.factorialhr.com/api/2026-01-01/resources"
HEADERS     = {"x-api-key": API_KEY, "Accept": "application/json"}

print("=" * 70)
print("🔍 EXPLORANDO FACTORIAL")
print("=" * 70)

if not API_KEY:
    print("❌ FACTORIAL_API_KEY no configurada en .env")
    exit(1)

# 1. Empleados
print("\n1️⃣  Empleados disponibles:")
r = requests.get(f"{BASE}/employees/employees", headers=HEADERS, params={"per_page": 20})
print(f"   Status: {r.status_code}")
if r.status_code == 200:
    for emp in r.json().get("data", []):
        a = emp.get("attributes", emp)
        name = f"{a.get('first_name', '')} {a.get('last_name', '')}".strip()
        print(f"   ID: {emp.get('id') or a.get('id')}  |  {name}  |  {a.get('email', '')}")
else:
    print(f"   ❌ {r.text[:300]}")

# 2. Gastos aprobados
print("\n2️⃣  Gastos aprobados (primeros 5):")
params = {
    "include_attachments": "true",
    "include_manual_drafts": "false",
    "include_grouped": "false",
    "status[]": "approved",
    "per_page": 5
}
if EMPLOYEE_ID:
    params["employee_ids[]"] = EMPLOYEE_ID

r2 = requests.get(f"{BASE}/expenses/expenses", headers=HEADERS, params=params)
print(f"   Status: {r2.status_code}")
if r2.status_code == 200:
    items = r2.json().get("data", [])
    print(f"   ✅ {len(items)} gasto(s)")
    for item in items:
        a = item.get("attributes", item)
        print(f"\n   --- Gasto ID {item.get('id')} ---")
        for k, v in a.items():
            print(f"   {k}: {repr(v)[:100]}")
else:
    print(f"   ❌ {r2.text[:300]}")

print("\n" + "=" * 70)
