import logging
import random
import re
import string
from datetime import date, datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from llm import LLMError, QwenClient
from parser import ParsedEvent, STATUS_ALIASES, fmt_frames, parse_message, parse_all
from stt import GroqSTT, STTError
from sync_engine import SyncEngine

logger = logging.getLogger(__name__)

# Callback data prefixes (all ≤ 64 bytes total when combined with payload)
_CB_MOVE = "mv:"        # mv:{token}
_CB_DEL = "dl:"         # dl:{frames_csv}
_CB_ASSIGN = "as:"      # as:{token}:{user_id}
_CB_BATCH_TOGGLE = "bt:"  # bt:{token}:{index}
_CB_BATCH_EXEC = "bx:"    # bx:{token}
_CB_DEL_MSG = "dm:"       # dm:{token}:{index}
_CB_NEWTASK_COL = "ntc:"  # ntc:{token}:{col_index}
_CB_DELCOL = "dco:"       # dco:{token}
_CB_SKIP = "sk"
_CB_CANCEL = "cx"

# user_data pending state types
_P_ADD = "add"
_P_COMMENT = "cmt"
_P_DESCRIBE = "dsc"
_P_DEADLINE = "ddl"
_P_PHOTO = "photo"
_P_NEWTASK = "nt"
_P_NEWCOL = "nc"


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


def _event_label(ev: ParsedEvent) -> str:
    frames_str = f"К{fmt_frames(ev.frames)}" if ev.frames else ""
    if ev.action == "move":
        return f"{frames_str} → {ev.target_status}"
    if ev.action == "delete":
        return f"🗑 {frames_str}"
    if ev.action == "add":
        if ev.frames:
            return f"➕ Создать Кадр {fmt_frames(ev.frames)}"
        return f"➕ {(ev.extra_text or 'новая задача')[:30]}"
    if ev.action == "comment_only":
        return f"💬 {frames_str}"
    if ev.action == "describe":
        return f"📝 {frames_str}"
    return frames_str


def _batch_keyboard(
    token: str, events: list[ParsedEvent], selected: list[bool]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, (ev, sel) in enumerate(zip(events, selected)):
        mark = "✅" if sel else "⬜"
        label = f"{mark} {_event_label(ev)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"bt:{token}:{i}")])
    n_sel = sum(selected)
    exec_label = f"▶ Выполнить ({n_sel})" if n_sel else "▶ Выполнить"
    rows.append([
        InlineKeyboardButton(exec_label, callback_data=f"bx:{token}"),
        InlineKeyboardButton("✕ Отмена", callback_data=_CB_CANCEL),
    ])
    return InlineKeyboardMarkup(rows)


async def _show_batch_checklist(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    events: list[ParsedEvent],
    header: str | None = None,
) -> None:
    selected = [True] * len(events)
    token = _store(ctx, {
        "events": [
            {
                "frames": ev.frames,
                "target_status": ev.target_status,
                "action": ev.action,
                "comment": ev.comment,
                "extra_text": ev.extra_text,
            }
            for ev in events
        ],
        "selected": selected,
    })
    if header is None:
        n = len(events)
        header = f"Найдено {n} {'команда' if n == 1 else 'команды' if n < 5 else 'команд'}. Выбери нужные:"
    await update.message.reply_text(
        header, reply_markup=_batch_keyboard(token, events, selected)
    )


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
    app.add_handler(CommandHandler("myid", _cmd_myid))
    app.add_handler(CommandHandler("report", _cmd_report))
    app.add_handler(CommandHandler("add", _cmd_add))
    app.add_handler(CommandHandler("newtask", _cmd_newtask))
    app.add_handler(CommandHandler("delete", _cmd_delete))
    app.add_handler(CommandHandler("rename", _cmd_rename))
    app.add_handler(CommandHandler("comment", _cmd_comment))
    app.add_handler(CommandHandler("desc", _cmd_desc))
    app.add_handler(CommandHandler("assign", _cmd_assign))
    app.add_handler(CommandHandler("deadline", _cmd_deadline))
    app.add_handler(CommandHandler("cancel", _cmd_cancel_pending))
    app.add_handler(CommandHandler("messages", _cmd_messages))
    app.add_handler(CommandHandler("cleardesc", _cmd_cleardesc))
    app.add_handler(CommandHandler("qwen", _cmd_qwen))
    app.add_handler(CommandHandler("newcol", _cmd_newcol))
    app.add_handler(CommandHandler("renamecol", _cmd_renamecol))
    app.add_handler(CommandHandler("deletecol", _cmd_deletecol))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _on_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, _on_photo))

    app.add_handler(CallbackQueryHandler(_cb_move, pattern=r"^mv:"))
    app.add_handler(CallbackQueryHandler(_cb_delete, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(_cb_assign, pattern=r"^as:"))
    app.add_handler(CallbackQueryHandler(_cb_batch_toggle, pattern=r"^bt:"))
    app.add_handler(CallbackQueryHandler(_cb_batch_exec, pattern=r"^bx:"))
    app.add_handler(CallbackQueryHandler(_cb_delete_message, pattern=r"^dm:"))
    app.add_handler(CallbackQueryHandler(_cb_qwen_confirm, pattern=r"^qw:"))
    app.add_handler(CallbackQueryHandler(_cb_newtask_col, pattern=r"^ntc:"))
    app.add_handler(CallbackQueryHandler(_cb_deletecol, pattern=r"^dco:"))
    app.add_handler(CallbackQueryHandler(_cb_skip, pattern=rf"^{_CB_SKIP}$"))
    app.add_handler(CallbackQueryHandler(_cb_cancel, pattern=rf"^{_CB_CANCEL}$"))


# ── helpers ────────────────────────────────────────────────────────────────

def _is_allowed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed: set[int] = ctx.bot_data["allowed"]
    return not allowed or update.effective_chat.id in allowed


def _engine(ctx: ContextTypes.DEFAULT_TYPE) -> SyncEngine:
    return ctx.bot_data["engine"]


def _actor(update: Update) -> str:
    user = update.effective_user
    if not user:
        return ""
    return user.full_name or user.username or str(user.id)


async def _is_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return True
    if chat.type == "private":
        return True
    try:
        member = await ctx.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _format_daily_report(actions: list[dict], date_str: str) -> str:
    if not actions:
        return f"Сводка за {date_str}: действий не было."

    moves: dict[str, list[int]] = {}
    created: list[str] = []
    deleted: list[int] = []
    comments: list[int] = []
    descriptions: list[int] = []

    for a in actions:
        frames = [int(f) for f in a["frames"].split(",") if f.isdigit()] if a.get("frames") else []
        action = a["action"]
        if action == "move":
            status = (a.get("details") or "").replace("→ ", "").strip()
            moves.setdefault(status, []).extend(frames)
        elif action == "add":
            created.append(a.get("details") or "?")
        elif action == "delete":
            deleted.extend(frames)
        elif action == "comment":
            comments.extend(frames)
        elif action == "describe":
            descriptions.extend(frames)

    lines = [f"Сводка за {date_str}"]
    if moves:
        lines.append("\nПеремещения:")
        for status, frs in moves.items():
            lines.append(f"  → {status}: {fmt_frames(sorted(set(frs)))}")
    if created:
        lines.append(f"\nСоздано ({len(created)}):")
        for t in created:
            lines.append(f"  • {t}")
    if deleted:
        lines.append(f"\nУдалено: {fmt_frames(sorted(set(deleted)))}")
    if comments:
        lines.append(f"\nКомментарии: {fmt_frames(sorted(set(comments)))}")
    if descriptions:
        lines.append(f"\nОписания обновлены: {fmt_frames(sorted(set(descriptions)))}")
    return "\n".join(lines)


async def daily_report_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id: int = ctx.bot_data.get("report_chat_id", 0)
    if not chat_id:
        return
    engine: SyncEngine = ctx.bot_data["engine"]
    actions = await engine.get_today_actions()
    date_str = date.today().strftime("%d.%m.%Y")
    text = _format_daily_report(actions, date_str)
    try:
        await ctx.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error("Не удалось отправить сводку: %s", e)


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
        "  /newtask [название] — создать задачу с выбором колонки\n"
        "  /rename <кадр> <название> — переименовать задачу\n"
        "  /delete <кадр>      — удалить задачу (только администраторы)\n"
        "  /comment <кадр> <текст>\n"
        "  /desc <кадр> <текст>\n"
        "  /assign <кадр>      — назначить исполнителя\n"
        "  /deadline <кадр> <ДД.ММ.ГГГГ>\n\n"
        "Управление колонками (только администраторы):\n"
        "  /newcol [название]  — создать колонку\n"
        "  /renamecol Старое|Новое — переименовать\n"
        "  /deletecol <название>   — удалить"
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


# ── /myid ─────────────────────────────────────────────────────────────────

async def _cmd_cancel_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if ctx.user_data.pop("pending", None):
        await update.message.reply_text("Отменено.")
    else:
        await update.message.reply_text("Нет активного ожидания.")


async def _cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"User ID: {uid}\nChat ID: {cid}\n\n"
        f"Для ежедневных сводок добавь в .env на VPS:\n"
        f"REPORT_CHAT_ID={uid}"
    )


# ── /report ────────────────────────────────────────────────────────────────

async def _cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    actions = await _engine(ctx).get_today_actions()
    date_str = date.today().strftime("%d.%m.%Y")
    await update.message.reply_text(_format_daily_report(actions, date_str))


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
        task = await _engine(ctx).create_task_with_title(title, actor=_actor(update))
        tid = task.get("id", "?")
        await msg.edit_text(f"Задача создана в Бэклоге:\n«{title}»\nid: {tid}")
    except Exception as e:
        await msg.edit_text(f"Ошибка создания: {e}")


# ── /newtask ───────────────────────────────────────────────────────────────

async def _cmd_newtask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    title = " ".join(ctx.args) if ctx.args else ""
    if title:
        await _newtask_show_columns(update, ctx, title)
    else:
        ctx.user_data["pending"] = {"type": _P_NEWTASK}
        await update.message.reply_text("Введите название новой задачи:")


async def _newtask_show_columns(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, title: str
) -> None:
    engine = _engine(ctx)
    cols = engine.get_column_names()
    if not cols:
        await update.message.reply_text("Нет данных о колонках — выполните /sync")
        return
    token = _store(ctx, {"name": title, "actor": _actor(update)})
    buttons: list[list[InlineKeyboardButton]] = []
    for i, col in enumerate(cols):
        cb = f"ntc:{token}:{i}"
        if len(cb.encode()) <= 64:
            buttons.append([InlineKeyboardButton(col, callback_data=cb)])
    buttons.append([InlineKeyboardButton("✕ Отмена", callback_data=_CB_CANCEL)])
    await update.message.reply_text(
        f"Задача: «{title}»\nВыберите колонку:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _cb_newtask_col(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    payload = query.data[len(_CB_NEWTASK_COL):]
    token, _, idx_str = payload.rpartition(":")
    idx = int(idx_str)
    data = _load(ctx, token)
    _drop(ctx, token)
    if not data:
        await query.edit_message_text("Сессия истекла, повторите /newtask.")
        return
    title = data["name"]
    actor = data.get("actor", "")
    engine = _engine(ctx)
    cols = engine.get_column_names()
    if idx >= len(cols):
        await query.edit_message_text("Ошибка: колонка не найдена, выполните /sync и повторите.")
        return
    col_name = cols[idx]
    try:
        task = await engine.create_task_in_column(title, col_name, actor=actor)
        await query.edit_message_text(
            f"Задача «{title}» создана в «{col_name}».\nid: {task.get('id', '?')}"
        )
    except Exception as e:
        await query.edit_message_text(f"Ошибка: {e}")


# ── /rename ────────────────────────────────────────────────────────────────

async def _cmd_rename(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    args = ctx.args
    if not args or not args[0].isdigit() or len(args) < 2:
        await update.message.reply_text("Использование: /rename <кадр> <новое название>")
        return
    frame = int(args[0])
    new_title = " ".join(args[1:])
    try:
        await _engine(ctx).rename_frame_task(frame, new_title, actor=_actor(update))
        await update.message.reply_text(f"Кадр {frame}: название изменено на «{new_title}»")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


# ── /newcol, /renamecol, /deletecol ───────────────────────────────────────

async def _cmd_newcol(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    if not await _is_admin(update, ctx):
        await update.message.reply_text("Управление колонками доступно только администраторам.")
        return
    name = " ".join(ctx.args) if ctx.args else ""
    if name:
        await _do_create_column(update, ctx, name)
    else:
        ctx.user_data["pending"] = {"type": _P_NEWCOL}
        await update.message.reply_text("Введите название новой колонки:")


async def _do_create_column(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, name: str
) -> None:
    try:
        col = await _engine(ctx).create_column_op(name)
        await update.message.reply_text(
            f"Колонка «{name}» создана.\nid: {col.get('id', '?')}"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def _cmd_renamecol(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    if not await _is_admin(update, ctx):
        await update.message.reply_text("Управление колонками доступно только администраторам.")
        return
    full = " ".join(ctx.args) if ctx.args else ""
    sep = "|" if "|" in full else ("→" if "→" in full else None)
    if not sep:
        await update.message.reply_text(
            "Использование: /renamecol <старое название>|<новое название>\n"
            "Пример: /renamecol Бэклог|Входящие"
        )
        return
    old, _, new = full.partition(sep)
    old, new = old.strip(), new.strip()
    if not old or not new:
        await update.message.reply_text("Укажите оба названия через «|».")
        return
    try:
        await _engine(ctx).rename_column_op(old, new)
        await update.message.reply_text(f"Колонка «{old}» переименована в «{new}».")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def _cmd_deletecol(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    if not await _is_admin(update, ctx):
        await update.message.reply_text("Удаление колонок доступно только администраторам.")
        return
    name = " ".join(ctx.args) if ctx.args else ""
    if not name:
        await update.message.reply_text("Использование: /deletecol <название колонки>")
        return
    token = _store(ctx, {"col": name})
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Да, удалить", callback_data=f"dco:{token}"),
        InlineKeyboardButton("Отмена", callback_data=_CB_CANCEL),
    ]])
    await update.message.reply_text(
        f"Удалить колонку «{name}»?\n"
        "⚠️ Задачи внутри останутся в YouGile, но без привязки к этой колонке.",
        reply_markup=kb,
    )


async def _cb_deletecol(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data[len(_CB_DELCOL):]
    data = _load(ctx, token)
    _drop(ctx, token)
    if not data:
        await query.edit_message_text("Сессия истекла.")
        return
    col_name = data["col"]
    try:
        await _engine(ctx).delete_column_op(col_name)
        await query.edit_message_text(f"Колонка «{col_name}» удалена.")
    except Exception as e:
        await query.edit_message_text(f"Ошибка: {e}")


# ── /delete ────────────────────────────────────────────────────────────────

async def _cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    if not await _is_admin(update, ctx):
        await update.message.reply_text("Удаление задач доступно только администраторам группы.")
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
            await engine.comment_frame(frame, text, actor=_actor(update))
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
        await _engine(ctx).update_description(frame, text, actor=_actor(update))
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


# ── photo handler ─────────────────────────────────────────────────────────

async def _on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return

    msg_obj = update.message
    if msg_obj.photo:
        tg_file_id = msg_obj.photo[-1].file_id  # largest size
        filename = "photo.jpg"
        mime = "image/jpeg"
    elif msg_obj.document and (msg_obj.document.mime_type or "").startswith("image/"):
        tg_file_id = msg_obj.document.file_id
        filename = msg_obj.document.file_name or "image"
        mime = msg_obj.document.mime_type or "image/jpeg"
    else:
        return

    caption = msg_obj.caption or ""
    event = parse_message(caption) if caption else None
    frame = event.frames[0] if (event and event.has_frames) else None

    if frame:
        await _do_attach_photo(update, ctx, tg_file_id, frame, filename, mime)
    else:
        ctx.user_data["pending"] = {
            "type": _P_PHOTO,
            "file_id": tg_file_id,
            "filename": filename,
            "mime": mime,
        }
        await msg_obj.reply_text(
            "К какому кадру прикрепить фото?\nВведите номер (например: 5):"
        )


async def _do_attach_photo(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    file_id: str, frame: int, filename: str, mime: str,
) -> None:
    import io as _io
    msg = await update.message.reply_text(f"Прикрепляю к кадру {frame}...")
    try:
        tg_file = await ctx.bot.get_file(file_id)
        buf = _io.BytesIO()
        await tg_file.download_to_memory(buf)
        data = buf.getvalue()

        url = await _engine(ctx).attach_photo_to_frame(
            frame, data, filename, mime, actor=_actor(update)
        )
        await msg.edit_text(f"Фото загружено и добавлено в чат кадра {frame}.\n{url}")
    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}")


# ── voice handler ──────────────────────────────────────────────────────────

async def _on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return

    stt: GroqSTT | None = ctx.bot_data.get("stt")
    if not stt:
        return  # молча игнорируем если GROQ_API_KEY не задан

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    msg = await update.message.reply_text("Распознаю голосовое...")
    try:
        tg_file = await ctx.bot.get_file(voice.file_id)
        import io as _io
        buf = _io.BytesIO()
        await tg_file.download_to_memory(buf)
        audio_bytes = buf.getvalue()

        text = await stt.transcribe(audio_bytes)
    except STTError as e:
        await msg.edit_text(f"Ошибка распознавания: {e}")
        return
    except Exception as e:
        await msg.edit_text(f"Ошибка загрузки аудио: {e}")
        return

    if not text:
        await msg.edit_text("Не удалось распознать речь.")
        return

    await msg.edit_text(f"Распознано: «{text}»\nОбрабатываю...")

    # Если в тексте есть "квен" / "qwen" — передаём в LLM
    if re.search(r"кв[эе]н|qwen", text, re.IGNORECASE):
        clean = re.sub(r"кв[эе]н|qwen", "", text, flags=re.IGNORECASE).strip()
        qwen: QwenClient | None = ctx.bot_data.get("qwen")
        if not qwen:
            await msg.edit_text(f"Распознано: «{text}»\n\nQWEN_API_KEY не задан.")
            return
        try:
            events = await qwen.parse_all(clean or text)
        except LLMError as e:
            await msg.edit_text(f"Распознано: «{text}»\n\nОшибка Qwen: {e}")
            return

        valid = [ev for ev in events if ev.frames or ev.action == "add"]
        if not valid:
            await msg.edit_text(f"Распознано: «{text}»\n\nQwen не нашёл команд.")
            return

        n = len(valid)
        await msg.edit_text(
            f"Распознано: «{text}»\n\n"
            f"Qwen распознал {n} {'команду' if n == 1 else 'команды' if n < 5 else 'команд'}:"
        )
        await _show_batch_checklist(
            update, ctx, valid,
            header="Выбери нужные и нажми Выполнить:",
        )
        return

    all_events = parse_all(text)

    if len(all_events) >= 2:
        await msg.edit_text(f"Распознано: «{text}»")
        await _show_batch_checklist(update, ctx, all_events)
        return

    event = all_events[0] if all_events else parse_message(text)

    if not event.has_frames:
        await msg.edit_text(f"Распознано: «{text}»\n\nКадры не найдены — уточните.")
        return

    if not event.has_status:
        await msg.edit_text(
            f"Распознано: «{text}»\n\n"
            f"Кадры: {fmt_frames(event.frames)} — статус не понят."
        )
        return

    result = await _engine(ctx).process_event(event)
    await msg.edit_text(f"Распознано: «{text}»\n\n{result.summary()}")


_CANCEL_PHRASES = {
    "нет", "не надо", "стоп", "стой", "отмена", "отменить",
    "cancel", "хватит", "не нужно", "не нужен", "не нужна",
    "нет нет", "стоп стоп",
}


async def _maybe_qwen_fallback(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
    fallback_text: str = "",
) -> None:
    """Call Qwen automatically when the parser can't understand a message.

    If Qwen is not configured, shows fallback_text (if provided) or stays silent.
    """
    qwen: QwenClient | None = ctx.bot_data.get("qwen")
    if not qwen:
        if fallback_text:
            await update.message.reply_text(fallback_text)
        return

    msg = await update.message.reply_text("Анализирую через Qwen...")
    try:
        events = await qwen.parse_all(text)
    except LLMError as e:
        logger.warning("Qwen auto-fallback error: %s", e)
        reply = fallback_text or "Не понял команду."
        await msg.edit_text(f"{reply}\n\nQwen тоже не смог распознать.")
        return

    valid = [ev for ev in events if ev.frames or ev.action == "add"]
    if not valid:
        reply = fallback_text or (
            "Не понял команду.\n\nПопробуй:\n"
            "  Кадр 5 готово\n"
            "  Кадры 1–10 у заказчика\n"
            "  /help — все команды"
        )
        await msg.edit_text(reply)
        return

    n = len(valid)
    await msg.edit_text(
        f"Qwen нашёл {n} {'команду' if n == 1 else 'команды' if n < 5 else 'команд'}:"
    )
    await _show_batch_checklist(update, ctx, valid, header="Выбери нужные и нажми Выполнить:")


# ── message handler ────────────────────────────────────────────────────────

async def _on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return

    text = update.message.text
    low = text.strip().lower()

    # Natural-language cancel: clears any pending state
    if low in _CANCEL_PHRASES:
        ctx.user_data.pop("pending", None)
        await update.message.reply_text("Отменено.")
        return

    pending = ctx.user_data.get("pending")

    if pending:
        # If the message looks like a real command (high-confidence event),
        # ignore the stale pending state so commands aren't swallowed as text.
        _peek = parse_message(text)
        if _peek.confidence >= 0.9:
            ctx.user_data.pop("pending", None)
            pending = None

    if pending:
        ptype = pending.get("type")
        ctx.user_data.pop("pending", None)

        if ptype == _P_ADD:
            await _do_add_task(update, ctx, text.strip())
            return

        if ptype == _P_NEWTASK:
            await _newtask_show_columns(update, ctx, text.strip())
            return

        if ptype == _P_NEWCOL:
            await _do_create_column(update, ctx, text.strip())
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

        if ptype == _P_PHOTO:
            raw = text.strip()
            if not raw.isdigit():
                await update.message.reply_text("Введите номер кадра цифрой, например: 5")
                return
            await _do_attach_photo(
                update, ctx,
                pending["file_id"], int(raw),
                pending["filename"], pending["mime"],
            )
            return

    all_events = parse_all(text)
    if len(all_events) >= 2:
        # Strip delete events for non-admins in group chats
        if any(ev.action == "delete" for ev in all_events):
            if not await _is_admin(update, ctx):
                all_events = [ev for ev in all_events if ev.action != "delete"]
                if not all_events:
                    await update.message.reply_text(
                        "Удаление задач доступно только администраторам группы."
                    )
                    return
        await _show_batch_checklist(update, ctx, all_events)
        return

    event = all_events[0] if all_events else parse_message(text)

    if event.action == "add":
        if event.has_frames:
            # Frames detected — build add-events per frame and show checklist
            add_events = [
                ParsedEvent(frames=[f], action="add", comment=f"Кадр {f:02d}")
                for f in event.frames
            ]
            await _show_batch_checklist(update, ctx, add_events)
        else:
            ctx.user_data["pending"] = {"type": _P_ADD}
            await update.message.reply_text("Введите название новой задачи:")
        return

    if event.action == "delete":
        if not event.has_frames:
            await update.message.reply_text(
                "Укажите кадр для удаления. Пример: «Удалить кадр 5»"
            )
            return
        if not await _is_admin(update, ctx):
            await update.message.reply_text("Удаление задач доступно только администраторам группы.")
            return
        await _show_batch_checklist(update, ctx, [event])
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
        await _maybe_qwen_fallback(update, ctx, text)
        return

    if not event.has_status:
        await _maybe_qwen_fallback(
            update, ctx, text,
            fallback_text=(
                f"Нашёл кадры: {fmt_frames(event.frames)} — но не понял статус.\n"
                "Уточните: «у заказчика», «на правках», «в работе», «готово»..."
            ),
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
    result = await engine.process_event(event, actor=_actor(update))
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


def _event_summary(event: ParsedEvent) -> str:
    action_labels = {
        "move": "Переместить",
        "delete": "Удалить",
        "add": "Создать задачу",
        "comment_only": "Комментарий",
        "describe": "Описание",
    }
    lines = [f"Действие: {action_labels.get(event.action, event.action)}"]
    if event.frames:
        lines.append(f"Кадры: {fmt_frames(event.frames)}")
    if event.target_status:
        lines.append(f"Статус: {event.target_status}")
    if event.extra_text:
        lines.append(f"Текст: {event.extra_text}")
    return "\n".join(lines)


async def _execute_event(
    reply_fn,
    ctx: ContextTypes.DEFAULT_TYPE,
    event: ParsedEvent,
) -> None:
    engine = _engine(ctx)

    if event.action == "move":
        result = await engine.process_event(event)
        await reply_fn(result.summary())
        return

    if event.action == "delete":
        lines: list[str] = []
        for frame in event.frames:
            try:
                title = await engine.delete_frame(frame)
                lines.append(f"{title} удалён")
            except Exception as e:
                lines.append(f"Кадр {frame}: {e}")
        await reply_fn("\n".join(lines) or "Готово")
        return

    if event.action == "add":
        try:
            if event.frames:
                results_add: list[str] = []
                for frame in event.frames:
                    t = f"Кадр {frame:02d}"
                    task = await engine.create_task_with_title(t)
                    results_add.append(f"Создано: «{t}»")
                await reply_fn("\n".join(results_add))
            else:
                title = event.extra_text or event.comment or "Новая задача"
                task = await engine.create_task_with_title(title)
                await reply_fn(f"Задача создана: «{title}» (id: {task.get('id', '?')})")
        except Exception as e:
            await reply_fn(f"Ошибка: {e}")
        return

    if event.action == "comment_only":
        lines = []
        for frame in event.frames:
            try:
                await engine.comment_frame(frame, event.extra_text or event.comment)
                lines.append(f"Кадр {frame}: комментарий добавлен")
            except Exception as e:
                lines.append(f"Кадр {frame}: {e}")
        await reply_fn("\n".join(lines))
        return

    if event.action == "describe":
        frame = event.frames[0] if event.frames else None
        if frame is None:
            await reply_fn("Не найден номер кадра для описания.")
            return
        try:
            await engine.update_description(frame, event.extra_text or event.comment)
            await reply_fn(f"Описание кадра {frame} обновлено.")
        except Exception as e:
            await reply_fn(f"Ошибка: {e}")


# ── /qwen ──────────────────────────────────────────────────────────────────

def _messages_keyboard(token: str, messages: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, msg in enumerate(messages):
        raw = (msg.get("text") or "").replace("\n", " ").strip()
        label = f"🗑 {raw[:45]}" if raw else "🗑 (вложение)"
        rows.append([InlineKeyboardButton(label, callback_data=f"dm:{token}:{i}")])
    rows.append([InlineKeyboardButton("✕ Закрыть", callback_data=_CB_CANCEL)])
    return InlineKeyboardMarkup(rows)


# ── /messages ──────────────────────────────────────────────────────────────

async def _cmd_messages(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /messages <кадр>")
        return
    frame = int(ctx.args[0])
    msg = await update.message.reply_text(f"Загружаю сообщения кадра {frame}...")
    try:
        task_id, messages = await _engine(ctx).get_task_messages(frame)
    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}")
        return

    if not messages:
        await msg.edit_text(f"Кадр {frame}: сообщений нет.")
        return

    # Keep last 15 messages
    shown = messages[-15:]
    token = _store(ctx, {"task_id": task_id, "messages": shown, "frame": frame})
    kb = _messages_keyboard(token, shown)
    await msg.edit_text(
        f"Кадр {frame} — последние {len(shown)} сообщений. Нажми 🗑 чтобы удалить:",
        reply_markup=kb,
    )


async def _cb_delete_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    payload = query.data[len(_CB_DEL_MSG):]
    token, _, idx_str = payload.rpartition(":")
    idx = int(idx_str)

    data = _load(ctx, token)
    if not data:
        await query.edit_message_text("Сессия истекла, повторите /messages.")
        return

    messages: list[dict] = data["messages"]
    task_id: str = data["task_id"]
    frame: int = data["frame"]

    if idx >= len(messages):
        await query.answer("Сообщение уже удалено.")
        return

    msg_id: int = messages[idx]["id"]
    try:
        await _engine(ctx).delete_task_message(task_id, msg_id)
    except Exception as e:
        await query.answer(f"Ошибка: {e}", show_alert=True)
        return

    messages.pop(idx)
    data["messages"] = messages

    if not messages:
        _drop(ctx, token)
        await query.edit_message_text(f"Кадр {frame}: все сообщения удалены.")
        return

    kb = _messages_keyboard(token, messages)
    await query.edit_message_text(
        f"Удалено. Кадр {frame} — осталось {len(messages)} сообщений:",
        reply_markup=kb,
    )


# ── /cleardesc ─────────────────────────────────────────────────────────────

async def _cmd_cleardesc(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("Использование: /cleardesc <кадр>")
        return
    frame = int(ctx.args[0])
    try:
        await _engine(ctx).clear_description(frame, actor=_actor(update))
        await update.message.reply_text(f"Описание кадра {frame} очищено.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


# ── /qwen ──────────────────────────────────────────────────────────────────

async def _cmd_qwen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update, ctx):
        return

    qwen: QwenClient | None = ctx.bot_data.get("qwen")
    if not qwen:
        await update.message.reply_text(
            "QWEN_API_KEY не задан. Добавьте в .env и перезапустите бот."
        )
        return

    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text("Использование: /qwen <текст сообщения>")
        return

    msg = await update.message.reply_text("Анализирую через Qwen...")
    try:
        events = await qwen.parse_all(text)
    except LLMError as e:
        await msg.edit_text(f"Ошибка Qwen:\n{e}")
        return

    valid = [
        ev for ev in events
        if ev.frames or ev.action == "add"
    ]
    if not valid:
        await msg.edit_text(
            "Qwen не нашёл команд в тексте.\n"
            f"Сырой ответ: {events}"
        )
        return

    n = len(valid)
    await msg.edit_text(
        f"Qwen распознал {n} {'команду' if n == 1 else 'команды' if n < 5 else 'команд'}:"
    )
    await _show_batch_checklist(
        update, ctx, valid,
        header="Выбери нужные и нажми Выполнить:",
    )


async def _cb_qwen_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    token = query.data[len("qw:"):]
    data = _load(ctx, token)
    _drop(ctx, token)
    if not data:
        await query.edit_message_text("Сессия истекла, повторите /qwen.")
        return

    event = ParsedEvent(
        frames=data["frames"],
        target_status=data["target_status"],
        comment=data["comment"],
        action=data["action"],
        extra_text=data["extra_text"],
        confidence=0.9,
    )

    await query.edit_message_text("Выполняю...")
    await _execute_event(
        lambda text: query.edit_message_text(text),
        ctx,
        event,
    )


async def _cb_batch_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    payload = query.data[len(_CB_BATCH_TOGGLE):]
    token, _, idx_str = payload.rpartition(":")
    idx = int(idx_str)

    data = _load(ctx, token)
    if not data:
        await query.edit_message_text("Сессия истекла, повторите команду.")
        return

    data["selected"][idx] = not data["selected"][idx]

    events = [
        ParsedEvent(
            frames=e["frames"],
            target_status=e["target_status"],
            action=e["action"],
            comment=e["comment"],
            extra_text=e["extra_text"],
        )
        for e in data["events"]
    ]
    await query.edit_message_reply_markup(
        reply_markup=_batch_keyboard(token, events, data["selected"])
    )


async def _cb_batch_exec(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    token = query.data[len(_CB_BATCH_EXEC):]
    data = _load(ctx, token)
    _drop(ctx, token)
    if not data:
        await query.edit_message_text("Сессия истекла, повторите команду.")
        return

    to_run = [
        ParsedEvent(
            frames=e["frames"],
            target_status=e["target_status"],
            action=e["action"],
            comment=e["comment"],
            extra_text=e["extra_text"],
        )
        for e, sel in zip(data["events"], data["selected"])
        if sel
    ]

    if not to_run:
        await query.edit_message_text("Ничего не выбрано.")
        return

    # Admin check: strip delete events for non-admins in group chats
    if any(ev.action == "delete" for ev in to_run):
        if not await _is_admin(update, ctx):
            to_run = [ev for ev in to_run if ev.action != "delete"]
            if not to_run:
                await query.edit_message_text(
                    "Удаление задач доступно только администраторам группы."
                )
                return

    await query.edit_message_text(f"Выполняю {len(to_run)} команд...")

    engine = _engine(ctx)
    parts: list[str] = []

    for ev in to_run:
        try:
            if ev.action == "move":
                res = await engine.process_event(ev)
                parts.append(res.summary())

            elif ev.action == "delete":
                lines: list[str] = []
                for frame in ev.frames:
                    title = await engine.delete_frame(frame)
                    lines.append(f"{title} удалён")
                parts.append("\n".join(lines))

            elif ev.action == "add":
                if ev.frames:
                    for frame in ev.frames:
                        t = f"Кадр {frame:02d}"
                        task = await engine.create_task_with_title(t)
                        parts.append(f"Создано: «{t}»")
                else:
                    title = ev.extra_text or ev.comment or "Новая задача"
                    task = await engine.create_task_with_title(title)
                    parts.append(f"Создано: «{title}»")

            elif ev.action == "comment_only":
                for frame in ev.frames:
                    await engine.comment_frame(frame, ev.extra_text or ev.comment)
                parts.append(f"Комментарий добавлен: {fmt_frames(ev.frames)}")

            elif ev.action == "describe":
                if ev.frames:
                    await engine.update_description(ev.frames[0], ev.extra_text or ev.comment)
                    parts.append(f"Описание кадра {ev.frames[0]} обновлено")

        except Exception as e:
            label = _event_label(ev)
            parts.append(f"Ошибка [{label}]: {e}")

    await query.edit_message_text("\n\n".join(parts) or "Готово")


async def _cb_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Пропущено.")


async def _cb_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("pending", None)
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Отменено.")
