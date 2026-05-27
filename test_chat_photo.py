#!/usr/bin/env python3
"""
Тест: встраивание фото прямо в чат задачи YouGile.
Запуск: python3 test_chat_photo.py
"""
import json, requests, base64, re
from pathlib import Path

KEY = BOARD_ID = ""
for line in Path(".env").read_text().splitlines():
    if line.startswith("YOUGILE_API_KEY="): KEY = line.split("=", 1)[1].strip()
    if line.startswith("YOUGILE_BOARD_ID="): BOARD_ID = line.split("=", 1)[1].strip()

BASE = "https://ru.yougile.com/api-v2"
H = {"Authorization": f"Bearer {KEY}"}

# ── найти задачу ───────────────────────────────────────────────────────────
cols = requests.get(f"{BASE}/columns", headers=H, params={"boardId": BOARD_ID}).json()
col_list = cols if isinstance(cols, list) else cols.get("content", [])
task_id = None
for col in col_list:
    t = requests.get(f"{BASE}/tasks", headers=H, params={"columnId": col["id"]}).json()
    t_list = t if isinstance(t, list) else t.get("content", [])
    if t_list:
        task_id = t_list[0]["id"]
        print(f"Задача: {t_list[0].get('title','?')}  id={task_id}\n")
        break

if not task_id:
    exit("Задачи не найдены")

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
)

# ── шаг 1: загружаем файл ─────────────────────────────────────────────────
print("=== POST /upload-file ===")
r = requests.post(f"{BASE}/upload-file", headers=H,
                  files={"file": ("test.png", PNG, "image/png")}, timeout=15)
print(f"  {r.status_code}: {r.text[:500]}")
if not r.ok:
    exit("Загрузка не удалась")

resp = r.json()
print(f"  Полный ответ: {json.dumps(resp, ensure_ascii=False)}")
full_url = resp.get("fullUrl", "")

# Извлекаем UUID из URL: .../user-data/{uuid}/filename
m = re.search(r"/user-data/([^/]+)/", full_url)
file_uuid = m.group(1) if m else None
print(f"  fullUrl: {full_url}")
print(f"  UUID из URL: {file_uuid}\n")

# ── шаг 2: пробуем разные форматы прикрепления в чате ─────────────────────
formats = [
    ("files=[uuid]",       {"text": "test", "files": [file_uuid]}),
    ("fileIds=[uuid]",     {"text": "test", "fileIds": [file_uuid]}),
    ("attachments=[uuid]", {"text": "test", "attachments": [file_uuid]}),
    ("files=[{uuid:...}]", {"text": "test", "files": [{"uuid": file_uuid}]}),
    ("files=[{id:...}]",   {"text": "test", "files": [{"id": file_uuid}]}),
    ("images=[uuid]",      {"text": "test", "images": [file_uuid]}),
    ("image=uuid",         {"text": "test", "image": file_uuid}),
    ("file=uuid",          {"text": "test", "file": file_uuid}),
]

for label, body in formats:
    rr = requests.post(
        f"{BASE}/chats/{task_id}/messages",
        headers={**H, "Content-Type": "application/json"},
        json=body,
        timeout=10,
    )
    print(f"[{label}] → {rr.status_code}: {rr.text[:200]}")

# ── шаг 3: multipart прямо в чат ──────────────────────────────────────────
print("\n=== Multipart напрямую в /chats/{id}/messages ===")
rr = requests.post(
    f"{BASE}/chats/{task_id}/messages",
    headers=H,
    files={"file": ("direct.png", PNG, "image/png")},
    data={"text": "multipart direct"},
    timeout=10,
)
print(f"  → {rr.status_code}: {rr.text[:300]}")

# ── шаг 4: смотрим схему messages из OpenAPI ──────────────────────────────
print("\n=== OpenAPI schema: POST /chats/{chatId}/messages ===")
spec_r = requests.get("https://ru.yougile.com/api-json", headers=H, timeout=15)
if spec_r.ok:
    spec = spec_r.json()
    chat_post = spec["paths"].get("/api-v2/chats/{chatId}/messages", {}).get("post", {})
    rb = chat_post.get("requestBody", {})
    print(json.dumps(rb, indent=2, ensure_ascii=False)[:2000])
else:
    print(f"  spec недоступен: {spec_r.status_code}")

print("\nГотово.")
