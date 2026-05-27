#!/usr/bin/env python3
"""Тест /api-v2/upload-file. Запуск: python3 fetch_docs.py"""
import json, requests, base64
from pathlib import Path

KEY = ""
BOARD_ID = ""
for line in Path(".env").read_text().splitlines():
    if line.startswith("YOUGILE_API_KEY="):
        KEY = line.split("=", 1)[1].strip()
    if line.startswith("YOUGILE_BOARD_ID="):
        BOARD_ID = line.split("=", 1)[1].strip()

BASE = "https://ru.yougile.com/api-v2"
H = {"Authorization": f"Bearer {KEY}"}

# Схема upload-file из спека
r = requests.get("https://ru.yougile.com/api-json", headers=H, timeout=10)
spec = r.json()
print("=== /api-v2/upload-file schema ===")
print(json.dumps(spec["paths"].get("/api-v2/upload-file", {}), indent=2, ensure_ascii=False))

print("\n=== /api-v2/chats/{chatId}/messages schema (POST) ===")
chat_post = spec["paths"].get("/api-v2/chats/{chatId}/messages", {}).get("post", {})
print(json.dumps(chat_post.get("requestBody", {}), indent=2, ensure_ascii=False))

print("\n=== /api-v2/tasks/{id} schema (PUT) ===")
task_put = spec["paths"].get("/api-v2/tasks/{id}", {}).get("put", {})
print(json.dumps(task_put.get("requestBody", {}), indent=2, ensure_ascii=False))

# Ищем задачу для теста
cols = requests.get(f"{BASE}/columns", headers=H, params={"boardId": BOARD_ID}).json()
col_list = cols if isinstance(cols, list) else cols.get("content", [])
task_id = None
for col in col_list:
    t = requests.get(f"{BASE}/tasks", headers=H, params={"columnId": col["id"]}).json()
    t_list = t if isinstance(t, list) else t.get("content", [])
    if t_list:
        task_id = t_list[0]["id"]
        print(f"\nТестовая задача: {t_list[0].get('title','?')} id={task_id}")
        break

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
)

# Пробуем загрузить файл
print("\n=== POST /api-v2/upload-file ===")
r = requests.post(f"{BASE}/upload-file", headers=H,
                  files={"file": ("test.png", PNG, "image/png")}, timeout=15)
print(f"  {r.status_code}: {r.text[:400]}")

if r.ok:
    data = r.json()
    print(f"  Ответ: {json.dumps(data, ensure_ascii=False)}")
    file_id = data.get("id") or data.get("uuid") or data.get("fileId")

    if file_id and task_id:
        print(f"\n=== Прикрепляем file_id={file_id} к задаче ===")
        # Вариант 1: через чат
        r2 = requests.post(f"{BASE}/chats/{task_id}/messages", headers=H,
            json={"text": "тест фото", "fileIds": [file_id]})
        print(f"  chats + fileIds: {r2.status_code} {r2.text[:200]}")

        r3 = requests.post(f"{BASE}/chats/{task_id}/messages", headers=H,
            json={"text": "тест фото", "files": [file_id]})
        print(f"  chats + files[]: {r3.status_code} {r3.text[:200]}")

        r4 = requests.post(f"{BASE}/chats/{task_id}/messages", headers=H,
            json={"text": "тест фото", "attachments": [file_id]})
        print(f"  chats + attachments[]: {r4.status_code} {r4.text[:200]}")
