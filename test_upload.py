#!/usr/bin/env python3
"""
Тест загрузки файла в YouGile. Запуск: python3 test_upload.py
"""
import os, sys, json, requests
from pathlib import Path

KEY = os.getenv("YOUGILE_API_KEY") or ""
BOARD_ID = os.getenv("YOUGILE_BOARD_ID") or ""
BASE = "https://ru.yougile.com/api-v2"

if not KEY or not BOARD_ID:
    # попробуем загрузить из .env
    for line in Path(".env").read_text().splitlines():
        if line.startswith("YOUGILE_API_KEY="):
            KEY = line.split("=", 1)[1].strip()
        if line.startswith("YOUGILE_BOARD_ID="):
            BOARD_ID = line.split("=", 1)[1].strip()

if not KEY:
    sys.exit("YOUGILE_API_KEY не найден")

H = {"Authorization": f"Bearer {KEY}"}

# ── найдём первую задачу на доске ─────────────────────────────────────────
cols = requests.get(f"{BASE}/columns", headers=H, params={"boardId": BOARD_ID}).json()
col_list = cols if isinstance(cols, list) else cols.get("content", [])
task_id = None
for col in col_list:
    tasks = requests.get(f"{BASE}/tasks", headers=H, params={"columnId": col["id"]}).json()
    t_list = tasks if isinstance(tasks, list) else tasks.get("content", [])
    if t_list:
        task_id = t_list[0]["id"]
        print(f"Используем задачу: {t_list[0].get('title','?')} (id={task_id})")
        break

if not task_id:
    sys.exit("Задачи не найдены")

# ── создаём тестовое изображение 1x1 px ──────────────────────────────────
import base64
# минимальный PNG 1x1 белый пиксель
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
)
img_bytes = PNG_1x1
img_name = "test.png"
img_mime = "image/png"

print(f"\nТестируем прикрепление файла к задаче {task_id}...")

# ── попытка 1: POST /tasks/{id}/attachments ───────────────────────────────
print("\n[1] POST /tasks/{id}/attachments (multipart)")
r = requests.post(
    f"{BASE}/tasks/{task_id}/attachments",
    headers=H,
    files={"file": (img_name, img_bytes, img_mime)},
)
print(f"    {r.status_code}: {r.text[:200]}")

# ── попытка 2: POST /chats/{id}/messages multipart + text ─────────────────
print("\n[2] POST /chats/{id}/messages (multipart + text)")
r = requests.post(
    f"{BASE}/chats/{task_id}/messages",
    headers=H,
    files={"file": (img_name, img_bytes, img_mime)},
    data={"text": "тест"},
)
print(f"    {r.status_code}: {r.text[:200]}")

# ── попытка 3: GET /chats/{id}/messages — смотрим структуру ──────────────
print("\n[3] GET /chats/{id}/messages — структура существующих сообщений")
r = requests.get(f"{BASE}/chats/{task_id}/messages", headers=H)
print(f"    {r.status_code}: {r.text[:400]}")

# ── попытка 4: POST /files ────────────────────────────────────────────────
print("\n[4] POST /files (multipart)")
r = requests.post(
    f"{BASE}/files",
    headers=H,
    files={"file": (img_name, img_bytes, img_mime)},
)
print(f"    {r.status_code}: {r.text[:200]}")

# ── попытка 5: POST /upload ───────────────────────────────────────────────
print("\n[5] POST /upload (multipart)")
r = requests.post(
    f"{BASE}/upload",
    headers=H,
    files={"file": (img_name, img_bytes, img_mime)},
)
print(f"    {r.status_code}: {r.text[:200]}")

# ── GET всех путей из api-json ────────────────────────────────────────────
print("\n[6] Ищем пути в api-json (file/attach/upload)...")
r = requests.get(f"{BASE}/api-json", headers=H)
if r.ok:
    spec = r.json()
    for path in sorted(spec.get("paths", {}).keys()):
        if any(kw in path.lower() for kw in ("file", "attach", "upload", "media")):
            print(f"    {path}")
else:
    print(f"    api-json: {r.status_code}")

print("\nГотово.")
