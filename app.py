#!/usr/bin/env python3
"""
Telegram Tutor Chat Finder — Web Interface
Multi-step wizard that authenticates with Telegram, then searches public
chats/channels related to math tutoring and exam prep (EGE/OGE).
"""

import asyncio
import json
import os
import re
import time
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, redirect, render_template, request, url_for, send_file

try:
    from telethon import TelegramClient
    from telethon.errors import (
        FloodWaitError,
        PasswordHashInvalidError,
        PhoneCodeInvalidError,
        SessionPasswordNeededError,
        UsernameInvalidError,
        UsernameNotOccupiedError,
    )
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.types import Channel, Chat
    TELETHON_OK = True
except ImportError:
    TELETHON_OK = False


app = Flask(__name__)
app.secret_key = os.urandom(24)

# ── Background event loop (all Telethon calls run here) ──────────────────────
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


def run_async(coro, timeout=60):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


# ── Global app state (single-user personal tool) ─────────────────────────────
class _State:
    def reset(self):
        self.client          = None
        self.phone           = None
        self.phone_code_hash = None
        self.step            = "setup"   # setup|phone|code|2fa|searching|done
        self.found           = {}
        self.log             = []        # append-only; SSE polls by index
        self.search_done     = False

    def __init__(self):
        self.reset()


S = _State()


def _emit(msg_type, **kw):
    S.log.append({"type": msg_type, **kw})


# ── Seed data ─────────────────────────────────────────────────────────────────

SEARCH_QUERIES = [
    "репетитор математика", "репетитор ЕГЭ", "репетитор ОГЭ",
    "подготовка ЕГЭ математика", "подготовка ОГЭ математика",
    "ЕГЭ математика", "ОГЭ математика", "уроки математики",
    "репетитор по математике", "подготовка к ЕГЭ", "подготовка к ОГЭ",
    "математика онлайн", "учитель математики", "алгебра репетитор",
    "геометрия репетитор",
    "math tutor", "maths tutor", "math exam prep",
    "algebra tutor", "math teacher online", "tutor math",
    "exam preparation math",
]

_PRIMARY_SEEDS = [
    "repetitor", "matematika", "ege", "oge", "podgotovka",
    "shkola", "algebra", "geometriya", "repmat", "matege",
    "egemat", "ogemat", "mathrep", "tutorege", "matprep",
    "tutor", "math", "maths", "mathtutor", "tutormath",
    "mathexam", "examprep", "mathprep", "mathhelp", "mathclass",
    "tutoring", "mathteacher", "studymath", "egemath", "ogemath",
    "mathonline", "onlinetutor", "onlinemath",
]

_SECONDARY_WORDS = [
    "math", "mat", "ege", "oge", "rep", "tutor",
    "online", "rus", "spb", "msk", "pro", "club",
]

REQUEST_DELAY = 1.5   # seconds between Telegram API calls


def _valid_username(name: str) -> bool:
    return bool(re.match(r'^[a-z0-9_]{5,32}$', name))


def _generate_usernames() -> list:
    seen: set = set()
    out: list = []

    def add(raw: str):
        name = raw.lower()
        if _valid_username(name) and name not in seen:
            seen.add(name)
            out.append(name)

    for seed in _PRIMARY_SEEDS:
        add(seed)
        for n in range(1, 51):
            add(f"{seed}{n}")
            add(f"{seed}_{n}")
        for n in range(1, 11):
            add(f"{n}{seed}")

    for seed in _PRIMARY_SEEDS:
        for word in _SECONDARY_WORDS:
            if seed != word:
                add(f"{seed}_{word}")
                add(f"{seed}{word}")
                add(f"{word}_{seed}")
                add(f"{word}{seed}")

    return out


def _make_result(entity, source: str) -> dict:
    if isinstance(entity, Channel):
        kind = "channel" if entity.broadcast else "supergroup"
    elif isinstance(entity, Chat):
        kind = "group"
    else:
        kind = "unknown"
    username = getattr(entity, "username", None)
    link = f"https://t.me/{username}" if username else f"https://t.me/c/{entity.id}"
    return {
        "id":       entity.id,
        "title":    getattr(entity, "title", str(entity.id)),
        "username": username,
        "kind":     kind,
        "members":  getattr(entity, "participants_count", None),
        "link":     link,
        "source":   source,
    }


def _save_txt():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with Path("found_chats.txt").open("w", encoding="utf-8") as f:
        f.write("Telegram Tutor Chat Finder — Results\n")
        f.write(f"Generated : {ts}\n")
        f.write(f"Total     : {len(S.found)} unique chats/channels\n")
        f.write("=" * 60 + "\n\n")
        for i, r in enumerate(S.found.values(), 1):
            f.write(f"[{i}]\nTitle    : {r['title']}\n")
            f.write(f"Type     : {r['kind']}\nLink     : {r['link']}\n")
            if r["username"]:
                f.write(f"Username : @{r['username']}\n")
            if r["members"] is not None:
                f.write(f"Members  : {r['members']:,}\n")
            f.write(f"Found via: {r['source']}\n\n" + "-" * 40 + "\n\n")


# ── Search coroutine ──────────────────────────────────────────────────────────

async def _do_search():
    S.search_done = False
    client = S.client

    # Phase 1: keyword search
    _emit("phase", text=f"Phase 1/2 — keyword search ({len(SEARCH_QUERIES)} queries)")
    for q in SEARCH_QUERIES:
        _emit("log", text=f'Searching: "{q}"')
        try:
            result = await client(SearchRequest(q=q, limit=50))
            new = 0
            for e in result.chats:
                if e.id not in S.found:
                    S.found[e.id] = _make_result(e, f'keyword: "{q}"')
                    new += 1
            _emit("log", text=f"  -> {len(result.chats)} results, {new} new | total: {len(S.found)}")
        except FloodWaitError as e:
            _emit("warn", text=f"  Rate limited — waiting {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
        except Exception as exc:
            _emit("warn", text=f"  Error: {exc}")
        await asyncio.sleep(REQUEST_DELAY)

    # Phase 2: brute-force
    candidates = _generate_usernames()
    eta_min = round(len(candidates) * REQUEST_DELAY / 60)
    _emit("phase", text=f"Phase 2/2 — username brute-force ({len(candidates)} candidates, ~{eta_min} min)")
    for i, name in enumerate(candidates, 1):
        if i % 50 == 0:
            pct = round(i / len(candidates) * 100)
            _emit("progress", value=pct, current=i, total=len(candidates), found=len(S.found))
        try:
            entity = await client.get_entity(name)
            if isinstance(entity, (Channel, Chat)) and entity.id not in S.found:
                S.found[entity.id] = _make_result(entity, f"brute-force: @{name}")
                _emit("found", text=f"@{name}  ->  {getattr(entity, 'title', '?')}")
        except (ValueError, UsernameNotOccupiedError, UsernameInvalidError):
            pass
        except FloodWaitError as e:
            _emit("warn", text=f"  Rate limited — waiting {e.seconds}s")
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            pass
        await asyncio.sleep(REQUEST_DELAY)

    _save_txt()
    S.step = "done"
    S.search_done = True
    _emit("done", count=len(S.found))


def _start_search():
    future = asyncio.run_coroutine_threadsafe(_do_search(), _loop)

    def _on_done(f):
        try:
            f.result()
        except Exception as e:
            _emit("error", text=str(e))
            S.search_done = True
            S.step = "done"

    future.add_done_callback(_on_done)


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if S.step == "done":
        return redirect(url_for("results"))
    if S.step == "searching":
        return redirect(url_for("searching"))
    return redirect(url_for("setup"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if not TELETHON_OK:
        return "Error: telethon not installed. Run: pip install telethon", 500
    error = None
    if request.method == "POST":
        try:
            api_id = int(request.form.get("api_id", "").strip())
        except ValueError:
            return render_template("setup.html", error="API ID must be a number.", current_step=1)
        api_hash = request.form.get("api_hash", "").strip()
        if not api_hash:
            return render_template("setup.html", error="API Hash is required.", current_step=1)
        try:
            async def _connect():
                c = TelegramClient("tutor_finder_web", api_id, api_hash)
                await c.connect()
                return c
            S.client = run_async(_connect())
        except Exception as e:
            return render_template("setup.html", error=str(e), current_step=1)
        # Skip auth steps if session file is still valid
        try:
            if run_async(S.client.is_user_authorized()):
                S.step = "searching"
                S.found = {}
                S.log = []
                S.search_done = False
                _start_search()
                return redirect(url_for("searching"))
        except Exception:
            pass
        S.step = "phone"
        return redirect(url_for("phone"))
    return render_template("setup.html", error=error, current_step=1)


@app.route("/phone", methods=["GET", "POST"])
def phone():
    error = None
    if request.method == "POST":
        phone_num = request.form.get("phone", "").strip()
        try:
            sent = run_async(S.client.send_code_request(phone_num))
            S.phone = phone_num
            S.phone_code_hash = sent.phone_code_hash
            S.step = "code"
            return redirect(url_for("verify"))
        except Exception as e:
            error = str(e)
    return render_template("phone.html", error=error, current_step=2)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    error = None
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        try:
            run_async(S.client.sign_in(S.phone, code, phone_code_hash=S.phone_code_hash))
            S.step = "searching"
            S.found = {}
            S.log = []
            S.search_done = False
            _start_search()
            return redirect(url_for("searching"))
        except SessionPasswordNeededError:
            S.step = "2fa"
            return redirect(url_for("two_fa"))
        except PhoneCodeInvalidError:
            error = "Invalid code — please try again."
        except Exception as e:
            error = str(e)
    return render_template("verify.html", error=error, current_step=3)


@app.route("/2fa", methods=["GET", "POST"])
def two_fa():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        try:
            run_async(S.client.sign_in(password=password))
            S.step = "searching"
            S.found = {}
            S.log = []
            S.search_done = False
            _start_search()
            return redirect(url_for("searching"))
        except PasswordHashInvalidError:
            error = "Wrong password — please try again."
        except Exception as e:
            error = str(e)
    return render_template("2fa.html", error=error, current_step=3)


@app.route("/searching")
def searching():
    return render_template("searching.html", current_step=4)


@app.route("/stream")
def stream():
    """SSE endpoint — streams S.log entries by polling an index."""
    def events():
        pos = 0
        idle = 0
        while True:
            if pos < len(S.log):
                entry = S.log[pos]
                yield f"data: {json.dumps(entry)}\n\n"
                pos += 1
                idle = 0
                if entry.get("type") == "done":
                    return
            else:
                idle += 1
                if idle % 10 == 0:          # keepalive every ~5s
                    yield ": keepalive\n\n"
                time.sleep(0.5)

    return Response(
        events(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/results")
def results():
    return render_template("results.html", results=list(S.found.values()), current_step=5)


@app.route("/download")
def download():
    path = Path("found_chats.txt")
    if path.exists():
        return send_file(str(path.resolve()), as_attachment=True, download_name="found_chats.txt")
    return "File not found", 404


@app.route("/restart")
def restart():
    if S.client:
        try:
            run_async(S.client.disconnect(), timeout=5)
        except Exception:
            pass
    S.reset()
    return redirect(url_for("setup"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
