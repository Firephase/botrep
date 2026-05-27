#!/usr/bin/env python3
"""Ищет документацию и файловые эндпоинты YouGile. Запуск: python3 fetch_docs.py"""
import requests
from pathlib import Path

KEY = ""
for line in Path(".env").read_text().splitlines():
    if line.startswith("YOUGILE_API_KEY="):
        KEY = line.split("=", 1)[1].strip()

BASE = "https://ru.yougile.com/api-v2"
H_BEARER  = {"Authorization": f"Bearer {KEY}"}
H_YOUGILE = {"Authorization": f"YOUGILE-KEY {KEY}"}

import base64
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
)

# Ищем spec
print("=== OpenAPI spec ===")
for path in ("/api-json", "/swagger.json", "/openapi.json", "/docs-json", "/swagger", "/docs"):
    r = requests.get(f"https://ru.yougile.com{path}", headers=H_BEARER, timeout=10)
    print(f"  GET {path}: {r.status_code}")
    if r.ok and "paths" in r.text:
        import json
        spec = r.json()
        for p in sorted(spec.get("paths", {})):
            print(f"    {p}")
        break

# Перебираем вероятные файловые эндпоинты
print("\n=== Файловые эндпоинты (POST multipart) ===")
upload_variants = [
    (f"{BASE}/files",            H_BEARER),
    (f"{BASE}/files",            H_YOUGILE),
    ("https://ru.yougile.com/files", H_BEARER),
    ("https://ru.yougile.com/files", H_YOUGILE),
    ("https://yougile.com/api-v2/files", H_YOUGILE),
    (f"{BASE}/attachments",      H_BEARER),
    (f"{BASE}/upload",           H_BEARER),
    (f"{BASE}/storage",          H_BEARER),
    ("https://storage.yougile.com/upload", H_BEARER),
    ("https://storage.yougile.com/upload", H_YOUGILE),
    ("https://files.yougile.com/upload",   H_BEARER),
    ("https://files.yougile.com/upload",   H_YOUGILE),
]

for url, headers in upload_variants:
    try:
        r = requests.post(url, headers=headers,
                         files={"file": ("test.png", PNG, "image/png")}, timeout=8)
        print(f"  {r.status_code}  {url}")
        if r.ok or r.status_code not in (404, 405):
            print(f"    → {r.text[:300]}")
    except Exception as e:
        print(f"  ERR  {url}: {e}")

# Смотрим что отдаёт GET на разные пути
print("\n=== GET разведка ===")
for path in ("/files", "/attachments", "/storage", "/upload", "/media"):
    r = requests.get(f"{BASE}{path}", headers=H_BEARER, timeout=8)
    if r.status_code != 404:
        print(f"  GET {path}: {r.status_code} → {r.text[:200]}")
