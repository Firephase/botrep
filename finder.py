#!/usr/bin/env python3
"""
Telegram Tutor Chat Finder
Searches public Telegram chats related to tutoring, math, and exam prep
using two strategies:
  1. Keyword search (Russian + English natural-language queries)
  2. Username brute-force (generated patterns resolved via the API)

Results are written to found_chats.txt.

Usage:
    python finder.py

Credentials are read from environment variables API_ID / API_HASH,
or entered interactively on first run. A .session file is saved so
you only need to authenticate once.
"""

import asyncio
import os
import re
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.errors import (
        FloodWaitError,
        SessionPasswordNeededError,
        UsernameInvalidError,
        UsernameNotOccupiedError,
    )
    from telethon.tl.functions.contacts import SearchRequest
    from telethon.tl.types import Channel, Chat
except ImportError:
    sys.exit("Error: telethon is not installed.\nRun: pip install telethon")


# ── Configuration ─────────────────────────────────────────────────────────────

SESSION_NAME  = "tutor_finder"
RESULTS_FILE  = "found_chats.txt"
REQUEST_DELAY = 1.5   # seconds between API calls (keeps us safe from flood bans)
SEARCH_LIMIT  = 50    # max results per keyword query


# ── Keyword queries ───────────────────────────────────────────────────────────

SEARCH_QUERIES = [
    # Russian
    "репетитор математика",
    "репетитор ЕГЭ",
    "репетитор ОГЭ",
    "подготовка ЕГЭ математика",
    "подготовка ОГЭ математика",
    "ЕГЭ математика",
    "ОГЭ математика",
    "уроки математики",
    "репетитор по математике",
    "подготовка к ЕГЭ",
    "подготовка к ОГЭ",
    "математика онлайн",
    "занятия математикой",
    "учитель математики",
    "школьная математика",
    "алгебра репетитор",
    "геометрия репетитор",
    # English
    "math tutor",
    "maths tutor",
    "math exam prep",
    "algebra tutor",
    "math teacher online",
    "tutor math",
]


# ── Username brute-force seeds ────────────────────────────────────────────────

_PRIMARY_SEEDS = [
    # Transliterated Russian
    "repetitor", "matematika", "ege", "oge", "podgotovka",
    "shkola", "algebra", "geometriya", "repmat", "matege",
    "egemat", "ogemat", "mathrep", "tutorege", "matprep",
    # English
    "tutor", "math", "maths", "mathtutor", "tutormath",
    "mathexam", "examprep", "mathprep", "mathhelp", "mathclass",
    "tutoring", "mathteacher", "studymath", "egemath", "ogemath",
    "mathonline", "onlinetutor", "onlinemath",
]

_SECONDARY_WORDS = [
    "math", "mat", "ege", "oge", "rep", "tutor",
    "online", "rus", "spb", "msk", "pro", "club",
]


def _valid_username(name: str) -> bool:
    """Telegram username rules: 5–32 chars, a-z/0-9/underscore."""
    return bool(re.match(r'^[a-z0-9_]{5,32}$', name))


def generate_usernames() -> list:
    seen: set = set()
    out: list = []

    def add(raw: str) -> None:
        name = raw.lower()
        if _valid_username(name) and name not in seen:
            seen.add(name)
            out.append(name)

    for seed in _PRIMARY_SEEDS:
        add(seed)
        for n in range(1, 51):       # suffix 1–50
            add(f"{seed}{n}")
            add(f"{seed}_{n}")
        for n in range(1, 11):       # prefix 1–10
            add(f"{n}{seed}")

    for seed in _PRIMARY_SEEDS:
        for word in _SECONDARY_WORDS:
            if seed != word:
                add(f"{seed}_{word}")
                add(f"{seed}{word}")
                add(f"{word}_{seed}")
                add(f"{word}{seed}")

    return out


# ── Result model ──────────────────────────────────────────────────────────────

class ChatResult:
    def __init__(self, entity, source: str):
        self.id       = entity.id
        self.title    = getattr(entity, "title", str(entity.id))
        self.username = getattr(entity, "username", None)
        self.members  = getattr(entity, "participants_count", None)
        self.source   = source

        if isinstance(entity, Channel):
            self.kind = "channel" if entity.broadcast else "supergroup"
        elif isinstance(entity, Chat):
            self.kind = "group"
        else:
            self.kind = "unknown"

    @property
    def link(self) -> str:
        if self.username:
            return f"https://t.me/{self.username}"
        return f"https://t.me/c/{self.id}"

    def to_text(self) -> str:
        lines = [
            f"Title    : {self.title}",
            f"Type     : {self.kind}",
            f"Link     : {self.link}",
        ]
        if self.username:
            lines.append(f"Username : @{self.username}")
        if self.members is not None:
            lines.append(f"Members  : {self.members:,}")
        lines.append(f"Found via: {self.source}")
        return "\n".join(lines)


# ── Search strategies ─────────────────────────────────────────────────────────

async def search_by_keywords(client: TelegramClient, found: dict) -> None:
    print(f"\n[1/2] Keyword search — {len(SEARCH_QUERIES)} queries")
    for query in SEARCH_QUERIES:
        print(f"  Searching: \"{query}\"", end=" ", flush=True)
        try:
            result = await client(SearchRequest(q=query, limit=SEARCH_LIMIT))
            new = 0
            for entity in result.chats:
                if entity.id not in found:
                    found[entity.id] = ChatResult(entity, f'keyword: "{query}"')
                    new += 1
            print(f"-> {len(result.chats)} hits, {new} new")
        except FloodWaitError as e:
            print(f"\n  Rate limited. Sleeping {e.seconds}s...")
            await asyncio.sleep(e.seconds + 1)
        except Exception as exc:
            print(f"\n  Error: {exc}")
        await asyncio.sleep(REQUEST_DELAY)


async def brute_force_usernames(client: TelegramClient, found: dict) -> None:
    candidates = generate_usernames()
    print(f"\n[2/2] Username brute-force — {len(candidates)} candidates")
    for i, name in enumerate(candidates, 1):
        if i % 100 == 0:
            pct = i / len(candidates) * 100
            print(f"  {i}/{len(candidates)} ({pct:.0f}%) | total found: {len(found)}")
        try:
            entity = await client.get_entity(name)
            if isinstance(entity, (Channel, Chat)) and entity.id not in found:
                found[entity.id] = ChatResult(entity, f"brute-force: @{name}")
                title = getattr(entity, "title", "?")
                print(f"  Found: @{name} -> {title}")
        except (ValueError, UsernameNotOccupiedError, UsernameInvalidError):
            pass
        except FloodWaitError as e:
            print(f"  Rate limited. Sleeping {e.seconds}s...")
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            pass
        await asyncio.sleep(REQUEST_DELAY)


# ── Output ────────────────────────────────────────────────────────────────────

def save_results(found: dict) -> None:
    path = Path(RESULTS_FILE)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("w", encoding="utf-8") as f:
        f.write("Telegram Tutor Chat Finder — Results\n")
        f.write(f"Generated : {timestamp}\n")
        f.write(f"Total     : {len(found)} unique chats/channels\n")
        f.write("=" * 60 + "\n\n")
        for idx, result in enumerate(found.values(), 1):
            f.write(f"[{idx}]\n")
            f.write(result.to_text())
            f.write("\n\n" + "-" * 40 + "\n\n")
    print(f"\nSaved to: {path.resolve()}")


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    print("=== Telegram Tutor Chat Finder ===\n")

    api_id_str = os.environ.get("API_ID", "").strip()
    api_hash   = os.environ.get("API_HASH", "").strip()

    if not api_id_str:
        api_id_str = input("API ID   (from my.telegram.org): ").strip()
    if not api_hash:
        api_hash = input("API Hash (from my.telegram.org): ").strip()

    try:
        api_id = int(api_id_str)
    except ValueError:
        sys.exit("Error: API ID must be an integer.")

    client = TelegramClient(SESSION_NAME, api_id, api_hash)
    print("\nConnecting to Telegram...")
    await client.start(
        phone=lambda: input("Phone number (e.g. +79001234567): "),
        password=lambda: getpass("2FA password (blank if none): ") or None,
        code_callback=lambda: input("Verification code: "),
    )
    print("Connected.\n")

    found: dict = {}
    await search_by_keywords(client, found)
    await brute_force_usernames(client, found)

    print(f"\nDone. Found {len(found)} unique chats/channels.")
    save_results(found)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
