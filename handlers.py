import logging
import random
import re
import string
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from parser import ParsedEvent, STATUS_ALIASES, fmt_frames, parse_message
from sync_engine import SyncEngine

logger = logging.getLogger(__name__)

# Callback data prefixes (all ≤ 64 bytes total when combined with payload)
_CB_MOVE = "mv:"        # mv:{token}
_CB_DEL = "dl:"         # dl:{frames_csv}
_CB_ASSIGN = "as:"      # as:{token}:{user_id}
_CB_SKIP = "sk"
_CB_CANCEL = "cx"

# user_data pending state types
_P_ADD = "add"
_P_COMMENT = "cmt"
_P_DESCRIBE = "dsc"
_P_DEADLINE = "ddl"


def _make_token() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _store(ctx: ContextTypes.DEFAULT_TYPE, data: dict) -> str:
    token = _make_token()
    ctx.bot_data.setdefault("_cb", {})[token] = data
    return token


def _load(ctx: ContextTypes.DEFAULT_TYPE, token: str) -> dict | None:
    return ctx.bot_data.get("_cb", {}).get(token)


def _drop(ctx: ContextTypes.DEFAULT_TYPE, token: str) -> None:
    ctx.bot_data.get("_cb", {}).pop(token, None)


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
    app.add_handler(CommandHandler("add", _cmd_add))
    app.add_handler(CommandHandler("delete", _cmd_delete))
    app.add_handler(CommandHandler("comment", _cmd_comment))
    app.add_handler(CommandHandler("desc", _cmd_desc))
    app.add_handler(CommandHandler("assign", _cmd_assign))
    app.add_handler(CommandHandler("deadline", _cmd_deadline))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))

    app.add_handler(CallbackQueryHandler(_cb_move, pattern=r"^mv:"))
    app.add_handler(CallbackQueryHandler(_cb_delete, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(_cb_assign, pattern=r"^as:"))
    app.add_handler(CallbackQueryHandler(_cb_skip, pattern=rf"^{_CB_SKIP}$"))
    app.add_handler(CallbackQueryHandler(_cb_cancel, pattern=rf"^{_CB_CANCEL}$"))


# ── helpers ────────────────────────────────────────────────────────────────

def _is_allowed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed: set[int] = ctx.bot_data["allowed"]
    return not allowed or update.effective_chat.id in allowed


def _engine(ctx: ContextTypes.DEFAULT_TYPE) -> SyncEngine:
    return ctx.bot_data["engine"]


def _parse_date(s: str) -> int | None:
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            dt = datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


async def _show_assign_keyboard(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    frames: list[int],
    prompt: str,
) -> None:
    engine = _engine(ctx)
    try:
        users = await engine.get_users()
    except Exception as e:
        logger.warning("get_users failed: %s", e)
        users = []

    if not users:
        return

    token = _store(ctx, {"frames": frames})
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for user in users[:16]:
        name = (
            user.get("name") or user.get("title")
            or user.get("login") or user.get("email", "?")
        )
        uid = user.get("id", "")
        cb = f"as:{token}:{uid}"
        if len(cb.encode()) <= 64:
            row.append(InlineKeyboardButton(name, callback_data=cb))
            if len(row) == 2:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("Пропустить", callback_data=_CB_SKIP)])

    await update.message.reply_text(prompt, reply_markup=InlineKeyboardMarkup(buttons))


# ── /start ─────────────────────────────────────────────────────────────────

async def _cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Бот YouGile синхронизации запущен.\n\n"
        "Примеры:\n"
        "  Кадры 1–18 отправила заказчику\n"
        "  Кадр 5 на правках\n"
        "  Удалить кадр 3\n"
        "  Комментарий к кадру 7: уточнить детали\n"
        "  Добавить задачу\n\n"
        "/help — все команды и статусы"
    )


# ── /help ──────────────────────────────────────────────────────────────────

async def _cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    statuses = "\n".join(f"  • {col}" for col in STATUS_ALIASES)
    await update.message.reply_text(
        "Форматы кадров:\n"
        "  Кадр 12  /  Кадры 1-18  /  Кадры 1, 4, 7\n\n"
        f"Статусы (пишите в свободной форме):\n{statuses}\n\n"
        "Действия в тексте:\n"
        "  Удалить кадр 5\n"
        "  Добавить задачу\n"
        "  Комментарий к кадру 5: текст\n"
        "  Описание кадра 5: новый текст\n\n"
        "Команды:\n"
        "  /sync               — обновить кэш из YouGile\n"
        "  /status             — колонки доски\n"
        "  /add [название]     — создать задачу в Бэклоге\n"
        "  /delete <кадр>      — удалить задачу\n"
        "  /comment <кадр> <текст>\n"
        "  /desc <кадр> <текст>\n"
        "  /assign <кадр>      — назначить исполнителя\n"
        "  /deadline <кадр> <ДД.ММ.ГГГГ>"
    )


# ── /sync ──────────────────────────────────────────────────────────────────

async def _cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    msg = await update.message.reply_text("Синхронизирую с YouGile...")
    try:
        n = await _engine(ctx).sync_board()
        await msg.edit_text(f"Готово. Кадров в кэше: {n}")
    except Exception as e:
        await msg.edit_text(f"Ошибка синхронизации: {e}")


# ── /status ────────────────────────────────────────────────────────────────

async def _cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    cols = _engine(ctx).get_column_names()
    text = "Колонки:\n" + "\n".join(f"  • {c}" for c in cols) if cols else "Нет данных — /sync"
    await update.message.reply_text(text)


# ── /add ───────────────────────────────────────────────────────────────────

async def _cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    title = " ".join(ctx.args) if ctx.args else ""
    if title:
        await _do_add_task(update, ctx, title)
    else:
        ctx.user_data["pending"] = {"type": _P_ADD}
        await update.message.reply_text("Введите название новой задачи:")


async def _do_add_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE, title: str) -> None:
    msg = await update.message.reply_text(f"Создаю задачу «{title}»...")
    try:
        task = await _engine(ctx).create_task_with_title(title)
        tid = task.get("id", "?")
        await msg.edit_text(f"Задача создана в Бэклоге:\n«{title}»\nid: {tid}")
    except Exception as e:
        await msg.edit_text(f"Ошибка создания: {e}")


# ── /delete ────────────────────────────────────────────────────────────────

async def _cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Использование: /delete <номер_кадра>")
        return
    frame = int(ctx.args[0])
    await _ask_delete_confirm(update, [frame])


async def _ask_delete_confirm(update: Update, frames: list[int]) -> None:
    frames_str = fmt_frames(frames)
    frames_csv = ",".join(str(f) for f in frames)
    cb = f"dl:{frames_csv}"
    if len(cb.encode()) > 64:
        await update.message.reply_text("Слишком много кадров для удаления за раз (макс. 20).")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Да, удалить", callback_data=cb),
        InlineKeyboardButton("Отмена", callback_data=_CB_CANCEL),
    ]])
    await update.message.reply_text(
        f"Удалить задачи кадров {frames_str} из YouGile?\nЭто действие необратимо.",
        reply_markup=kb,
    )


# ── /comment ───────────────────────────────────────────────────────────────

async def _cmd_comment(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /comment <кадр> <текст>")
        return
    frame = int(args[0])
    text = " ".join(args[1:])
    if text:
        await _do_comment(update, ctx, [frame], text)
    else:
        ctx.user_data["pending"] = {"type": _P_COMMENT, "frames": [frame]}
        await update.message.reply_text(f"Введите комментарий к кадру {frame}:")


async def _do_comment(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, frames: list[int], text: str
) -> None:
    engine = _engine(ctx)
    lines: list[str] = []
    for frame in frames:
        try:
            await engine.comment_frame(frame, text)
            lines.append(f"Кадр {frame}: комментарий добавлен")
        except Exception as e:
            lines.append(f"Кадр {frame}: ошибка — {e}")
    await update.message.reply_text("\n".join(lines))


# ── /desc ──────────────────────────────────────────────────────────────────

async def _cmd_desc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /desc <кадр> <текст>")
        return
    frame = int(args[0])
    text = " ".join(args[1:])
    if text:
        await _do_describe(update, ctx, frame, text)
    else:
        ctx.user_data["pending"] = {"type": _P_DESCRIBE, "frame": frame}
        await update.message.reply_text(f"Введите описание для кадра {frame}:")


async def _do_describe(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, frame: int, text: str
) -> None:
    try:
        await _engine(ctx).update_description(frame, text)
        await update.message.reply_text(f"Описание кадра {frame} обновлено.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


# ── /assign ────────────────────────────────────────────────────────────────

async def _cmd_assign(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /assign <кадр>")
        return
    frame = int(ctx.args[0])
    await _show_assign_keyboard(update, ctx, [frame], f"Назначить исполнителя на кадр {frame}:")


# ── /deadline ──────────────────────────────────────────────────────────────

async def _cmd_deadline(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Использование: /deadline <кадр> <ДД.ММ.ГГГГ>")
        return
    frame = int(args[0])
    if len(args) > 1:
        ts = _parse_date(args[1])
        if ts:
            await _do_deadline(update, ctx, frame, ts, args[1])
        else:
            await update.message.reply_text(f"Не распознал дату «{args[1]}». Формат: ДД.ММ.ГГГГ")
    else:
        ctx.user_data["pending"] = {"type": _P_DEADLINE, "frame": frame}
        await update.message.reply_text(f"Введите дедлайн для кадра {frame} (ДД.ММ.ГГГГ):")


async def _do_deadline(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, frame: int, ts_ms: int, label: str
) -> None:
    try:
        await _engine(ctx).set_deadline(frame, ts_ms)
        await update.message.reply_text(f"Дедлайн кадра {frame}: {label}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


# ── message handler ────────────────────────────────────────────────────────

async def _on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return

    text = update.message.text
    pending = ctx.user_data.get("pending")

    if pending:
        ptype = pending.get("type")
        ctx.user_data.pop("pending", None)

        if ptype == _P_ADD:
            await _do_add_task(update, ctx, text.strip())
            return

        if ptype == _P_COMMENT:
            await _do_comment(update, ctx, pending.get("frames", []), text.strip())
            return

        if ptype == _P_DESCRIBE:
            await _do_describe(update, ctx, pending["frame"], text.strip())
            return

        if ptype == _P_DEADLINE:
            ts = _parse_date(text.strip())
            if ts:
                await _do_deadline(update, ctx, pending["frame"], ts, text.strip())
            else:
                await update.message.reply_text("Не распознал дату. Формат: ДД.ММ.ГГГГ")
            return

    event = parse_message(text)

    if event.action == "add":
        ctx.user_data["pending"] = {"type": _P_ADD}
        await update.message.reply_text("Введите название новой задачи:")
        return

    if event.action == "delete":
        if not event.has_frames:
            await update.message.reply_text(
                "Укажите кадр для удаления. Пример: «Удалить кадр 5»"
            )
            return
        await _ask_delete_confirm(update, event.frames)
        return

    if event.action == "comment_only":
        if not event.has_frames:
            await update.message.reply_text(
                "Укажите кадр. Пример: «Комментарий к кадру 5: текст»"
            )
            return
        if event.extra_text:
            await _do_comment(update, ctx, event.frames, event.extra_text)
        else:
            ctx.user_data["pending"] = {"type": _P_COMMENT, "frames": event.frames}
            await update.message.reply_text(
                f"Введите текст комментария к кадрам {fmt_frames(event.frames)}:"
            )
        return

    if event.action == "describe":
        if not event.has_frames:
            await update.message.reply_text(
                "Укажите кадр. Пример: «Описание кадра 5: новый текст»"
            )
            return
        frame = event.frames[0]
        if event.extra_text:
            await _do_describe(update, ctx, frame, event.extra_text)
        else:
            ctx.user_data["pending"] = {"type": _P_DESCRIBE, "frame": frame}
            await update.message.reply_text(f"Введите описание для кадра {frame}:")
        return

    # Default: move action
    if not event.has_frames:
        return

    if not event.has_status:
        await update.message.reply_text(
            f"Нашёл кадры: {fmt_frames(event.frames)} — но не понял статус.\n"
            "Уточните: «у заказчика», «на правках», «в работе», «готово»..."
        )
        return

    limit: int = ctx.bot_data["limit"]
    if len(event.frames) > limit:
        token = _store(ctx, {"text": text})
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Да, обновить", callback_data=f"mv:{token}"),
            InlineKeyboardButton("Отмена", callback_data=_CB_CANCEL),
        ]])
        await update.message.reply_text(
            f"Большой диапазон: {len(event.frames)} кадров → {event.target_status}\nПодтвердить?",
            reply_markup=kb,
        )
        return

    await _apply_move(update, ctx, event)


# ── move ───────────────────────────────────────────────────────────────────

async def _apply_move(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, event: ParsedEvent
) -> None:
    engine = _engine(ctx)
    msg = await update.message.reply_text(
        f"Обновляю {len(event.frames)} кадров → {event.target_status}..."
    )
    result = await engine.process_event(event)
    await msg.edit_text(result.summary())

    if result.updated:
        await _show_assign_keyboard(
            update, ctx, result.updated,
            f"Назначить исполнителя на {fmt_frames(result.updated)}?"
        )


# ── callback handlers ──────────────────────────────────────────────────────

async def _cb_move(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data[len("mv:"):]
    data = _load(ctx, token)
    _drop(ctx, token)
    if not data:
        await query.edit_message_text("Сессия истекла, повторите команду.")
        return
    event = parse_message(data["text"])
    engine = _engine(ctx)
    await query.edit_message_text(f"Обновляю {len(event.frames)} кадров...")
    result = await engine.process_event(event)
    await query.edit_message_text(result.summary())


async def _cb_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    frames_csv = query.data[len("dl:"):]
    frames = [int(f) for f in frames_csv.split(",") if f.lstrip("-").isdigit()]
    engine = _engine(ctx)
    lines: list[str] = []
    for frame in frames:
        try:
            title = await engine.delete_frame(frame)
            lines.append(f"{title} удалён")
        except Exception as e:
            lines.append(f"Кадр {frame}: {e}")
    await query.edit_message_text("\n".join(lines) or "Готово")


async def _cb_assign(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    payload = query.data[len("as:"):]
    token, _, user_id = payload.partition(":")
    data = _load(ctx, token)
    _drop(ctx, token)
    if not data:
        await query.edit_message_text("Сессия истекла.")
        return
    frames = data.get("frames", [])
    engine = _engine(ctx)
    lines: list[str] = []
    for frame in frames:
        try:
            await engine.set_assignee(frame, user_id)
            lines.append(f"Кадр {frame}: назначен")
        except Exception as e:
            lines.append(f"Кадр {frame}: {e}")
    await query.edit_message_text("Исполнитель:\n" + "\n".join(lines))


async def _cb_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Пропущено.")


async def _cb_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("pending", None)
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Отменено.")
