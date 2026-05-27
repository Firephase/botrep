#!/usr/bin/env python3
"""Извлекает все эндпоинты из официальной документации YouGile. Запуск: python3 fetch_docs.py"""
import os, json, requests
from pathlib import Path

KEY = ""
for line in Path(".env").read_text().splitlines():
    if line.startswith("YOUGILE_API_KEY="):
        KEY = line.split("=", 1)[1].strip()

BASE = "https://ru.yougile.com/api-v2"
H = {"Authorization": f"Bearer {KEY}"}

# Загружаем OpenAPI spec
r = requests.get(f"{BASE}/api-json", headers=H, timeout=15)
print(f"api-json: {r.status_code}")
if not r.ok:
    print(r.text[:200])
    exit(1)

spec = r.json()
paths = spec.get("paths", {})
print(f"\nВсего эндпоинтов: {len(paths)}\n")

# Все эндпоинты
print("=== ВСЕ ПУТИ ===")
for path in sorted(paths):
    methods = list(paths[path].keys())
    print(f"  {', '.join(m.upper() for m in methods):20s}  {path}")

# Детали по файлам/вложениям/чату
print("\n=== ДЕТАЛИ: file/attach/upload/chat/message/upload ===")
keywords = ("file", "attach", "upload", "chat", "message", "media", "image")
for path, methods in paths.items():
    if any(kw in path.lower() for kw in keywords):
        for method, info in methods.items():
            print(f"\n{method.upper()} {path}")
            body = info.get("requestBody", {})
            if body:
                content = body.get("content", {})
                for ct, schema in content.items():
                    print(f"  Content-Type: {ct}")
                    props = schema.get("schema", {}).get("properties", {})
                    for k, v in props.items():
                        print(f"    {k}: {v.get('type','?')} {v.get('format','')}")
            params = info.get("parameters", [])
            for p in params:
                print(f"  param: {p.get('name')} ({p.get('in')})")
