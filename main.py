import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application

from datetime import time as dt_time

from config import Config
from database import Database
from handlers import daily_report_job, register
from llm import QwenClient
from stt import GroqSTT
from sync_engine import SyncEngine
from yougile import YouGileClient

logger = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    Path("data").mkdir(exist_ok=True)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("data/bot.log", encoding="utf-8"),
        ],
    )


# ── первичная настройка ────────────────────────────────────────────────────

async def _wizard() -> None:
    print("\nYouGile-Telegram Sync Bot — первичная настройка")
    print("─" * 48)
    print("Для выхода нажмите Ctrl+C\n")

    token = input("Telegram Bot Token: ").strip()
    api_key = input("YouGile API Key: ").strip()

    print("\nПодключаюсь к YouGile...")
    client = YouGileClient(api_key)
    await client.start()
    try:
        info = await client.test_connection()
        print(f"Подключение успешно ({info})")
    except Exception as e:
        print(f"Ошибка подключения к YouGile: {e}")
        await client.stop()
        sys.exit(1)

    projects = await client.get_projects()
    if not projects:
        print("Нет доступных проектов.")
        await client.stop()
        sys.exit(1)

    print("\nПроекты:")
    for i, p in enumerate(projects, 1):
        name = p.get("title") or p.get("name") or p["id"]
        print(f"  {i}. {name}")
    idx = int(input(f"Выберите проект (1–{len(projects)}): ")) - 1
    proj = projects[idx]
    proj_id = proj["id"]
    proj_name = proj.get("title") or proj.get("name") or str(proj_id)

    boards = await client.get_boards(proj_id)
    if not boards:
        print("Нет доступных досок в проекте.")
        await client.stop()
        sys.exit(1)

    print("\nДоски:")
    for i, b in enumerate(boards, 1):
        name = b.get("title") or b.get("name") or b["id"]
        print(f"  {i}. {name}")
    idx = int(input(f"Выберите доску (1–{len(boards)}): ")) - 1
    board = boards[idx]
    board_id = board["id"]
    board_name = board.get("title") or board.get("name") or board_id

    await client.stop()

    proj_key = re.sub(r"[^a-z0-9]+", "_", proj_name.lower()).strip("_")[:30]
    Path(".env").write_text(
        f"TELEGRAM_BOT_TOKEN={token}\n"
        f"YOUGILE_API_KEY={api_key}\n"
        f"YOUGILE_BOARD_ID={board_id}\n"
        f"YOUGILE_PROJECT_KEY={proj_key}\n"
        f"DB_PATH=data/bot.db\n"
        f"LOG_LEVEL=INFO\n"
        f"ALLOWED_CHAT_IDS=\n"
        f"LARGE_RANGE_LIMIT=50\n",
        encoding="utf-8",
    )
    print(f"\nКонфиг сохранён в .env")
    print(f"  Проект : {proj_name}")
    print(f"  Доска  : {board_name}")
    print(f"  ID     : {board_id}\n")


# ── точка входа ────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    cfg = Config.from_env()

    if not cfg.telegram_token or not cfg.yougile_api_key or not cfg.yougile_board_id:
        asyncio.run(_wizard())
        load_dotenv(override=True)
        cfg = Config.from_env()

    try:
        cfg.validate()
    except ValueError as e:
        print(e)
        sys.exit(1)

    _setup_logging(cfg.log_level)
    logger.info("Запуск бота...")

    db = Database(cfg.db_path)
    yougile = YouGileClient(cfg.yougile_api_key)
    engine = SyncEngine(yougile, db, cfg.yougile_board_id, cfg.yougile_project_key)
    qwen = QwenClient(cfg.qwen_api_key, cfg.qwen_model) if cfg.qwen_api_key else None
    stt = GroqSTT(cfg.groq_api_key, cfg.groq_model) if cfg.groq_api_key else None

    async def on_startup(_app: Application) -> None:
        await db.init()
        await yougile.start()
        logger.info("YouGile: %s", await yougile.test_connection())
        if qwen:
            await qwen.start()
            _app.bot_data["qwen"] = qwen
            logger.info("Qwen API подключён (модель: %s)", cfg.qwen_model)
        if stt:
            await stt.start()
            _app.bot_data["stt"] = stt
            logger.info("Groq STT подключён (модель: %s)", cfg.groq_model)
        try:
            n = await engine.sync_board()
            logger.info("Начальная синхронизация: %d кадров", n)
        except Exception as e:
            logger.warning("Синхронизация при старте не удалась: %s", e)

    async def on_shutdown(_app: Application) -> None:
        await yougile.stop()
        if qwen:
            await qwen.stop()
        if stt:
            await stt.stop()

    app = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    register(app, engine, cfg.allowed_chat_ids, cfg.large_range_limit)

    if cfg.report_chat_id:
        app.bot_data["report_chat_id"] = cfg.report_chat_id
        h, m = map(int, cfg.report_time.split(":"))
        app.job_queue.run_daily(
            daily_report_job,
            time=dt_time(hour=h, minute=m, tzinfo=timezone.utc),
            name="daily_report",
        )
        logger.info("Ежедневная сводка: %s UTC → chat %d", cfg.report_time, cfg.report_chat_id)

    logger.info("Бот запущен. Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
