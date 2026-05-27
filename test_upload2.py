#!/usr/bin/env python3
"""
Тест нативной загрузки файлов YouGile.
Запуск: python3 test_upload2.py
"""
import os, sys, json, requests
from pathlib import Path

KEY = ""
BOARD_ID = ""
for line in Path(".env").read_text().splitlines():
    if line.startswith("YOUGILE_API_KEY="):
        KEY = line.split("=", 1)[1].strip()
    if line.startswith("YOUGILE_BOARD_ID="):
        BOARD_ID = line.split("=", 1)[1].strip()

if not KEY:
    sys.exit("YOUGILE_API_KEY не найден в .env")

BASE_API = "https://ru.yougile.com/api-v2"
BASE_SITE = "https://yougile.com"

H_BEARER = {"Authorization": f"Bearer {KEY}"}
H_YOUGILE = {"Authorization": f"YOUGILE-KEY {KEY}"}

# Минимальный PNG 1x1
import base64
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
)

# ── находим task_id ────────────────────────────────────────────────────────
cols = requests.get(f"{BASE_API}/columns", headers=H_BEARER, params={"boardId": BOARD_ID}).json()
col_list = cols if isinstance(cols, list) else cols.get("content", [])
task_id = None
for col in col_list:
    t = requests.get(f"{BASE_API}/tasks", headers=H_BEARER, params={"columnId": col["id"]}).json()
    t_list = t if isinstance(t, list) else t.get("content", [])
    if t_list:
        task_id = t_list[0]["id"]
        print(f"Задача: {t_list[0].get('title','?')}  id={task_id}\n")
        break

if not task_id:
    sys.exit("Задачи не найдены")

# ── Шаг 1: пробуем загрузить файл ─────────────────────────────────────────
upload_paths = [
    (BASE_SITE,           H_YOUGILE,  "POST на yougile.com с YOUGILE-KEY"),
    (BASE_SITE,           H_BEARER,   "POST на yougile.com с Bearer"),
    (f"{BASE_SITE}/api-v2/files",  H_YOUGILE,  "POST /api-v2/files с YOUGILE-KEY"),
    (f"{BASE_SITE}/api-v2/files",  H_BEARER,   "POST /api-v2/files с Bearer"),
    (f"{BASE_API}/files",          H_YOUGILE,  "POST ru.yougile /files с YOUGILE-KEY"),
    (f"{BASE_API}/files",          H_BEARER,   "POST ru.yougile /files с Bearer"),
    (f"{BASE_SITE}/upload",        H_YOUGILE,  "POST yougile.com/upload с YOUGILE-KEY"),
]

file_uuid = None
for url, headers, label in upload_paths:
    print(f"[upload] {label}")
    try:
        r = requests.post(url, headers=headers,
                         files={"file": ("test.png", PNG, "image/png")}, timeout=10)
        print(f"  → {r.status_code}: {r.text[:200]}")
        if r.ok:
            try:
                data = r.json()
                file_uuid = data.get("uuid") or data.get("id") or data.get("fileId")
                if file_uuid:
                    print(f"  ✅ uuid={file_uuid}")
                    break
            except Exception:
                print(f"  ответ не JSON: {r.text[:100]}")
    except Exception as e:
        print(f"  ошибка: {e}")

if not file_uuid:
    print("\nФайл не удалось загрузить ни одним способом.")
    sys.exit(0)

# ── Шаг 2а: /api-v2/messages ──────────────────────────────────────────────
print(f"\n[attach 2a] POST /api-v2/messages с uuid={file_uuid}")
r = requests.post(f"{BASE_API}/messages", headers={**H_BEARER, "Content-Type": "application/json"},
    json={"taskId": task_id, "text": "тест", "files": [file_uuid]})
print(f"  → {r.status_code}: {r.text[:300]}")

r2 = requests.post(f"{BASE_API}/messages", headers={**H_BEARER, "Content-Type": "application/json"},
    json={"taskId": task_id, "files": [{"uuid": file_uuid}]})
print(f"  → {r2.status_code}: {r2.text[:300]}")

# ── Шаг 2б: PUT /tasks/{id} с attachments ─────────────────────────────────
print(f"\n[attach 2b] PUT /tasks/{task_id} с attachments")
r3 = requests.put(f"{BASE_API}/tasks/{task_id}",
    headers={**H_BEARER, "Content-Type": "application/json"},
    json={"attachments": [file_uuid]})
print(f"  → {r3.status_code}: {r3.text[:300]}")

r4 = requests.put(f"{BASE_API}/tasks/{task_id}",
    headers={**H_BEARER, "Content-Type": "application/json"},
    json={"files": [{"uuid": file_uuid}]})
print(f"  → {r4.status_code}: {r4.text[:300]}")

# ── Шаг 2в: POST /chats/{id}/messages с uuid ──────────────────────────────
print(f"\n[attach 2c] POST /chats/{task_id}/messages с uuid")
r5 = requests.post(f"{BASE_API}/chats/{task_id}/messages",
    headers={**H_BEARER, "Content-Type": "application/json"},
    json={"text": "тест фото", "files": [file_uuid]})
print(f"  → {r5.status_code}: {r5.text[:300]}")

r6 = requests.post(f"{BASE_API}/chats/{task_id}/messages",
    headers={**H_BEARER, "Content-Type": "application/json"},
    json={"text": "тест фото", "attachments": [{"uuid": file_uuid}]})
print(f"  → {r6.status_code}: {r6.text[:300]}")

print("\nГотово.")
