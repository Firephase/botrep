#!/usr/bin/env python3
"""
Тест встраивания фото через properties.params.chunks в YouGile чате.
Запуск: python3 test_chunks.py
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
        print(f"Задача id={task_id}\n")
        break

if not task_id:
    exit("Задачи не найдены")

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
)

# ── 1. загружаем файл ──────────────────────────────────────────────────────
r = requests.post(f"{BASE}/upload-file", headers=H,
                  files={"file": ("photo.png", PNG, "image/png")}, timeout=15)
resp = r.json()
full_url = resp["fullUrl"]
rel_url = resp["url"]  # /user-data/{uuid}/filename
m = re.search(r"/user-data/([^/]+)/", full_url)
uuid = m.group(1) if m else ""
print(f"Загружено: {full_url}\nUUID: {uuid}\n")

# ── 2. пробуем разные форматы chunks ──────────────────────────────────────
chunk_variants = [
    ("image type + fullUrl",
     [{"type": "image", "url": full_url}]),
    ("image type + rel url",
     [{"type": "image", "url": rel_url}]),
    ("image type + uuid",
     [{"type": "image", "uuid": uuid}]),
    ("file type + fullUrl",
     [{"type": "file", "url": full_url, "name": "photo.png"}]),
    ("attachment type",
     [{"type": "attachment", "url": full_url, "name": "photo.png"}]),
    ("img type",
     [{"type": "img", "url": full_url}]),
    ("photo type",
     [{"type": "photo", "url": full_url}]),
    ("uuid only in chunk",
     [{"uuid": uuid, "name": "photo.png", "mime": "image/png"}]),
]

for label, chunks in chunk_variants:
    body = {
        "text": f"тест [{label}]",
        "properties": {"params": {"chunks": chunks}},
    }
    rr = requests.post(
        f"{BASE}/chats/{task_id}/messages",
        headers={**H, "Content-Type": "application/json"},
        json=body,
        timeout=10,
    )
    print(f"[{label}] → {rr.status_code}: {rr.text[:200]}")

# ── 3. после отправки — смотрим последние сообщения ───────────────────────
print("\n=== Последние сообщения в чате ===")
msgs = requests.get(f"{BASE}/chats/{task_id}/messages", headers=H).json()
for msg in msgs.get("content", [])[-5:]:
    print(json.dumps(msg, ensure_ascii=False, indent=2)[:400])
    print()

# ── 4. показываем полную CreateChatMessageDto схему ───────────────────────
print("=== CreateChatMessageDto ===")
spec = requests.get("https://ru.yougile.com/api-json", headers=H, timeout=15).json()
dto = spec.get("components", {}).get("schemas", {}).get("CreateChatMessageDto", {})
print(json.dumps(dto, indent=2, ensure_ascii=False))

print("\nГотово.")
