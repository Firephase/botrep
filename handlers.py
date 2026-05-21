import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from parser import ParsedEvent, fmt_frames, parse_message
from sync_engine import SyncEngine

logger = logging.getLogger(__name__)

_CONFIRM = "confirm:"
_CANCEL = "cancel"


def register(
    app: Application,
    engine: SyncEngine,
    allowed_chat_ids: list[int],
    large_range_limit: int,
) -> None:
    app.bot_data.update(
        engine=engine,
        allowed=set(allowed_chat_ids),
        limit=large_range_limit,
    )
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_help))
    app.add_handler(CommandHandler("sync", _cmd_sync))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    app.add_handler(CallbackQueryHandler(_on_confirm, pattern=rf"^{_CONFIRM}"))
    app.add_handler(CallbackQueryHandler(_on_cancel, pattern=rf"^{_CANCEL}$"))


def _is_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed: set[int] = context.bot_data["allowed"]
    return not allowed or update.effective_chat.id in allowed


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Бот YouGile синхронизации запущен.\n\n"
        "Примеры:\n"
        "  Кадры 1–18 отправила заказчику\n"
        "  Кадр 5 правки\n"
        "  Кадры 1, 3, 7 готово\n\n"
        "/help — подробнее   /sync — обновить кэш"
    )


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Форматы кадров:\n"
        "  Кадр 12\n"
        "  Кадры 1-18  /  1–18\n"
        "  Кадры 1, 4, 7\n"
        "  Кадры 1-5, 8, 12-14\n\n"
        "Статусы:\n"
        "  Бэклог / В работе / У заказчика / На правках / Готово\n\n"
        "Команды:\n"
        "  /sync   — синхронизировать кэш из YouGile\n"
        "  /status — показать колонки доски"
    )


async def _cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    engine: SyncEngine = context.bot_data["engine"]
    msg = await update.message.reply_text("Синхронизирую с YouGile...")
    try:
        n = await engine.sync_board()
        await msg.edit_text(f"Готово. Кадров в кэше: {n}")
    except Exception as e:
        await msg.edit_text(f"Ошибка синхронизации: {e}")


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    engine: SyncEngine = context.bot_data["engine"]
    cols = engine.get_column_names()
    text = "Колонки:\n" + "\n".join(f"  • {c}" for c in cols) if cols else "Нет данных — /sync"
    await update.message.reply_text(text)


async def _on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, context):
        return
    text = update.message.text
    event = parse_message(text)

    if not event.has_frames:
        return  # не про кадры — игнорируем

    if not event.has_status:
        await update.message.reply_text(
            f"Нашёл кадры: {fmt_frames(event.frames)} — но не понял статус.\n"
            "Уточните: «у заказчика», «готово», «на правках», «в работе»..."
        )
        return

    limit: int = context.bot_data["limit"]
    if len(event.frames) > limit:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, обновить", callback_data=f"{_CONFIRM}{text[:200]}"),
            InlineKeyboardButton("Отмена", callback_data=_CANCEL),
        ]])
        await update.message.reply_text(
            f"Большой диапазон: {len(event.frames)} кадров → {event.target_status}\nПодтвердить?",
            reply_markup=kb,
        )
        return

    await _apply(update, context, event)


async def _apply(update: Update, context: ContextTypes.DEFAULT_TYPE, event: ParsedEvent) -> None:
    engine: SyncEngine = context.bot_data["engine"]
    msg = await update.message.reply_text(
        f"Обновляю {len(event.frames)} кадров → {event.target_status}..."
    )
    result = await engine.process_event(event)
    await msg.edit_text(result.summary())


async def _on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text = query.data[len(_CONFIRM):]
    event = parse_message(text)
    engine: SyncEngine = context.bot_data["engine"]
    await query.edit_message_text(f"Обновляю {len(event.frames)} кадров...")
    result = await engine.process_event(event)
    await query.edit_message_text(result.summary())


async def _on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Отменено.")
