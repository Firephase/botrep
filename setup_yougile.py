#!/usr/bin/env python3
"""
Создаёт структуру YouGile:
  - Колонки: Бэклог, В работе, У заказчика, На правках, Готово
  - Карточки: ML в нейромедицине / Кадр 01 … Кадр 20  (в колонке Бэклог)

Запуск:  python3 setup_yougile.py
"""

import json
import sys
import requests

# ── настройки ──────────────────────────────────────────────────────────────
KEY      = "asg6mwAlE0vrwDJmGedn769AiY7fJB6Ow6wq2BCs24me9jXCwlcgsZJMuhzfOaH-"
BOARD_ID = "57c67eed-d1cf-4473-b4d0-eb11f8b57a28"
BASE     = "https://ru.yougile.com/api-v2"

PROJECT_NAME = "ML в нейромедицине"
FRAME_COUNT  = 20
COLUMNS      = ["Бэклог", "В работе", "У заказчика", "На правках", "Готово"]
# ───────────────────────────────────────────────────────────────────────────

H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def get(path, **params):
    r = requests.get(f"{BASE}{path}", headers=H, params=params, timeout=15)
    return r


def post(path, body):
    r = requests.post(f"{BASE}{path}", headers=H, json=body, timeout=15)
    return r


def list_content(r):
    if not r.ok:
        return []
    d = r.json()
    if isinstance(d, list):
        return d
    for k in ("content", "data", "items"):
        if isinstance(d.get(k), list):
            return d[k]
    return []


# ── шаг 1: разведка эндпоинтов ─────────────────────────────────────────────
print("=" * 55)
print("Шаг 1: ищем рабочие эндпоинты")
print("=" * 55)

# Пробуем получить OpenAPI-спецификацию
spec_r = requests.get("https://ru.yougile.com/api-json", timeout=10)
if spec_r.ok:
    paths = list(spec_r.json().get("paths", {}).keys())
    kw = ("column", "task", "board", "sticker", "card")
    relevant = [p for p in paths if any(w in p.lower() for w in kw)]
    print("Эндпоинты из спецификации:")
    for p in sorted(relevant)[:30]:
        print(f"  {p}")
else:
    print(f"Спецификация недоступна ({spec_r.status_code}), пробуем вручную...")

# Пробуем разные варианты колонок
print("\nПоиск эндпоинта колонок:")
COL_CANDIDATES = [
    ("/string-board-columns", {"boardId": BOARD_ID}),
    ("/columns",              {"boardId": BOARD_ID}),
    ("/stickers",             {"boardId": BOARD_ID}),
    (f"/string-boards/{BOARD_ID}/columns", {}),
    (f"/string-boards/{BOARD_ID}", {}),
]
col_endpoint = None
col_param    = None
existing_cols: list[dict] = []

for ep, params in COL_CANDIDATES:
    r = get(ep, **params)
    status = r.status_code
    snippet = r.text[:120].replace("\n", " ")
    print(f"  {ep}: {status} | {snippet}")
    if r.ok:
        items = list_content(r)
        # Если вернулся dict с columns внутри
        if not items and isinstance(r.json(), dict):
            items = r.json().get("columns", [])
        if items is not None:
            col_endpoint = ep
            col_param    = params
            existing_cols = items if items else []
            print(f"  → РАБОТАЕТ (объектов: {len(existing_cols)})")
            break

if col_endpoint is None:
    print("\nНе нашли эндпоинт колонок. Вывод разведки выше — скинь его разработчику.")
    sys.exit(1)

# Пробуем разные варианты задач
print("\nПоиск эндпоинта задач:")
TASK_CANDIDATES = [
    ("/string-board-tasks", {"boardId": BOARD_ID}),
    ("/tasks",              {"boardId": BOARD_ID}),
    ("/cards",              {"boardId": BOARD_ID}),
]
task_endpoint = None
task_param    = None
existing_tasks: list[dict] = []

for ep, params in TASK_CANDIDATES:
    r = get(ep, **params)
    status = r.status_code
    snippet = r.text[:120].replace("\n", " ")
    print(f"  {ep}: {status} | {snippet}")
    if r.ok:
        items = list_content(r)
        if items is not None:
            task_endpoint = ep
            task_param    = params
            existing_tasks = items if items else []
            print(f"  → РАБОТАЕТ (задач: {len(existing_tasks)})")
            break

if task_endpoint is None:
    print("\nНе нашли эндпоинт задач. Скинь вывод разработчику.")
    sys.exit(1)

# ── шаг 2: создаём колонки ─────────────────────────────────────────────────
print("\n" + "=" * 55)
print("Шаг 2: создаём колонки")
print("=" * 55)

existing_col_titles = {c.get("title", "") for c in existing_cols}
print(f"Уже есть: {existing_col_titles or '—'}")

col_create_ep = col_endpoint.split("?")[0]  # без параметров
col_ids: dict[str, str] = {c["title"]: c["id"] for c in existing_cols if "id" in c}

COLORS = {
    "Бэклог":      "#95a5a6",
    "В работе":    "#3498db",
    "У заказчика": "#9b59b6",
    "На правках":  "#e67e22",
    "Готово":      "#2ecc71",
}

for col_name in COLUMNS:
    if col_name in existing_col_titles:
        print(f"  {col_name}: уже есть (id={col_ids.get(col_name, '?')})")
        continue

    body = {"boardId": BOARD_ID, "title": col_name, "color": COLORS.get(col_name, "#95a5a6")}
    r = post(col_create_ep, body)
    if r.ok:
        new_id = r.json().get("id", "?")
        col_ids[col_name] = new_id
        print(f"  {col_name}: создана (id={new_id})")
    else:
        print(f"  {col_name}: ОШИБКА {r.status_code} — {r.text[:200]}")

backlog_id = col_ids.get("Бэклог")
if not backlog_id:
    print("\nНет id колонки 'Бэклог' — не можем создать карточки.")
    sys.exit(1)

# ── шаг 3: создаём карточки ────────────────────────────────────────────────
print("\n" + "=" * 55)
print("Шаг 3: создаём карточки")
print("=" * 55)

existing_titles = {t.get("title", "") for t in existing_tasks}
task_create_ep  = task_endpoint.split("?")[0]

created = 0
skipped = 0

for i in range(1, FRAME_COUNT + 1):
    title = f"{PROJECT_NAME} / Кадр {i:02d}"
    if title in existing_titles:
        print(f"  {title}: уже есть")
        skipped += 1
        continue

    body = {"title": title, "columnId": backlog_id}
    r = post(task_create_ep, body)
    if r.ok:
        print(f"  {title}: создана")
        created += 1
    else:
        print(f"  {title}: ОШИБКА {r.status_code} — {r.text[:150]}")

print(f"\nГотово. Создано: {created}, пропущено (уже были): {skipped}")
print("\nТеперь запусти /sync в Telegram-боте чтобы обновить кэш.")
