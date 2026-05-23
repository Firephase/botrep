#!/usr/bin/env python3
"""
Создаёт структуру YouGile:
  - Колонки: Бэклог, В работе, У заказчика, На правках, Готово
  - Карточки: ML в нейромедицине / Кадр 01 … Кадр 20  (в колонке Бэклог)

Запуск:  python3 setup_yougile.py
"""

import sys
import requests

# ── настройки ──────────────────────────────────────────────────────────────
KEY        = "asg6mwAlE0vrwDJmGedn769AiY7fJB6Ow6wq2BCs24me9jXCwlcgsZJMuhzfOaH-"
PROJECT_ID = "57c67eed-d1cf-4473-b4d0-eb11f8b57a28"
BASE       = "https://ru.yougile.com/api-v2"

PROJECT_NAME = "ML в нейромедицине"
FRAME_COUNT  = 20
COLUMNS = ["Бэклог", "В работе", "У заказчика", "На правках", "Готово"]
COLORS  = {
    "Бэклог":      1,
    "В работе":    3,
    "У заказчика": 6,
    "На правках":  9,
    "Готово":      2,
}
# ───────────────────────────────────────────────────────────────────────────

H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def get(path, **params):
    return requests.get(f"{BASE}{path}", headers=H,
                        params={k: v for k, v in params.items() if v is not None},
                        timeout=15)


def post(path, body):
    return requests.post(f"{BASE}{path}", headers=H, json=body, timeout=15)


def content(r):
    d = r.json()
    if isinstance(d, list):
        return d
    for k in ("content", "data", "items"):
        if isinstance(d.get(k), list):
            return d[k]
    return []


# ── шаг 1: находим реальный ID доски ──────────────────────────────────────
print("=" * 55)
print("Шаг 1: ищем доску проекта")
print("=" * 55)

r = get("/boards", projectId=PROJECT_ID)
if not r.ok:
    print(f"Ошибка /boards: {r.status_code} — {r.text[:200]}")
    sys.exit(1)

boards = content(r)
print(f"Найдено досок: {len(boards)}")
for b in boards:
    print(f"  id={b['id']}  title={b.get('title', '?')}  type={b.get('type', '?')}")

# Ищем строковую (канбан) доску
board = next((b for b in boards if b.get("type") in ("string", "kanban", None)), None)
if not board and boards:
    board = boards[0]

if not board:
    print("Нет досок в проекте. Создай доску в YouGile и запусти снова.")
    sys.exit(1)

BOARD_ID = board["id"]
print(f"\nИспользуем доску: {board.get('title', '?')} (id={BOARD_ID})")
print(f"\n*** Сохрани в .env: YOUGILE_BOARD_ID={BOARD_ID} ***\n")

# ── шаг 2: создаём колонки ────────────────────────────────────────────────
print("=" * 55)
print("Шаг 2: создаём колонки")
print("=" * 55)

r = get("/columns", boardId=BOARD_ID)
if r.ok:
    existing = content(r)
else:
    print(f"Не удалось получить колонки: {r.status_code} — {r.text[:150]}")
    existing = []

existing_titles = {c.get("title", "") for c in existing}
col_ids: dict[str, str] = {c["title"]: c["id"] for c in existing if "id" in c}
print(f"Уже есть: {existing_titles or '—'}")

for name in COLUMNS:
    if name in existing_titles:
        print(f"  {name}: уже есть")
        continue
    r = post("/columns", {"boardId": BOARD_ID, "title": name, "color": COLORS[name]})
    if r.ok:
        new_id = r.json().get("id", "?")
        col_ids[name] = new_id
        print(f"  {name}: создана (id={new_id})")
    else:
        print(f"  {name}: ОШИБКА {r.status_code} — {r.text[:200]}")

backlog_id = col_ids.get("Бэклог")
if not backlog_id:
    print("\nНет id колонки 'Бэклог' — нельзя создать карточки.")
    sys.exit(1)

# ── шаг 3: создаём карточки ────────────────────────────────────────────────
print("\n" + "=" * 55)
print("Шаг 3: создаём карточки")
print("=" * 55)

r = get("/string-stickers", boardId=BOARD_ID)
if r.ok:
    existing_tasks = content(r)
    existing_task_titles = {t.get("title", "") for t in existing_tasks}
else:
    print(f"Не удалось получить карточки: {r.status_code} — {r.text[:150]}")
    existing_task_titles = set()

created = skipped = errors = 0

for i in range(1, FRAME_COUNT + 1):
    title = f"{PROJECT_NAME} / Кадр {i:02d}"
    if title in existing_task_titles:
        print(f"  {title}: уже есть")
        skipped += 1
        continue

    # Создаём стикер — только name, остальное через states
    r = post("/string-stickers", {"name": title})
    if not r.ok:
        print(f"  {title}: ОШИБКА создания {r.status_code} — {r.text[:150]}")
        errors += 1
        continue

    sticker_id = r.json().get("id")
    if not sticker_id:
        print(f"  {title}: нет id в ответе — {r.text[:100]}")
        errors += 1
        continue

    # Помещаем в колонку Бэклог через states
    rs = post(f"/string-stickers/{sticker_id}/states", {"columnId": backlog_id})
    if rs.ok:
        print(f"  {title}: создана ✓")
        created += 1
    else:
        # Если states не работает — стикер всё равно создан, просто без колонки
        print(f"  {title}: создана (колонка не задана: {rs.status_code} — {rs.text[:100]})")
        created += 1

print(f"\nГотово. Создано: {created}, пропущено: {skipped}, ошибок: {errors}")
print(f"\n*** Не забудь обновить YOUGILE_BOARD_ID={BOARD_ID} в .env на VPS ***")
print("Затем: docker-compose restart bot")
print("И отправь боту: /sync")
