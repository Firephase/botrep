import re
from dataclasses import dataclass, field

STATUS_ALIASES: dict[str, list[str]] = {
    "Бэклог": ["бэклог", "запланировано", "в очереди"],
    "В работе": [
        "в работе", "взял", "взяла", "делаю", "делаем",
        "начал", "начала", "работаю", "работаем",
    ],
    "У заказчика": [
        "заказчику", "клиенту", "у клиента", "у заказчика",
        "отправила заказчику", "отправил заказчику", "отправили заказчику",
        "отправила клиенту", "отправил клиенту",
        "ушло клиенту", "ушло заказчику",
    ],
    "На правках": [
        "правки", "поправить", "надо исправить", "исправить",
        "поправки", "на правках", "надо поправить", "доработать",
        "надо изменить", "на доработке",
    ],
    "Готово": [
        "готово", "принято", "апрув", "approved",
        "финал", "финальный", "сдано",
    ],
    "На внутренней проверке": [
        "на проверку", "проверьте", "чекните", "проверка",
        "на проверке", "внутренняя проверка",
    ],
    "На цветокоре": [
        "цвет", "цветокор", "цветокоррекция", "на цветокоре", "цветокорр",
    ],
    "На анимации": [
        "анимация", "анимирую", "анимировать", "на анимации", "анимируем",
    ],
}

_DELETE_WORDS = ["удалить", "удали", "убрать", "убери", "снести", "сноси"]
_ADD_WORDS = ["добавить", "добавь", "создать", "создай"]
_COMMENT_WORDS = ["комментарий", "заметка"]
_DESCRIBE_WORDS = ["описание", "изменить описание"]

_FRAME_WORD = r"(?:кадр(?:ы|ов|у|е|а|ом|ам|ах|ами)?|шот(?:ы|ов)?|сцен(?:а|ы|у)?|frame[s]?)"
_NUM_RANGE = r"\d+\s*[-–—]\s*\d+"
_NUM_SINGLE = r"\d+"
_NUM_LIST = rf"(?:{_NUM_RANGE}|{_NUM_SINGLE})(?:\s*,\s*(?:{_NUM_RANGE}|{_NUM_SINGLE}))*"

_PATTERNS = [
    rf"{_FRAME_WORD}\s+с\s+({_NUM_SINGLE})\s+по\s+({_NUM_SINGLE})",
    rf"{_FRAME_WORD}\s+({_NUM_LIST})",
    rf"({_NUM_LIST})\s+{_FRAME_WORD}",
    r"#?к(\d+)\b",
]

_INLINE_TEXT_RE = re.compile(r":\s*(.+)$", re.DOTALL)


@dataclass
class ParsedEvent:
    frames: list[int] = field(default_factory=list)
    target_status: str | None = None
    comment: str = ""
    action: str = "move"       # move | delete | add | comment_only | describe
    extra_text: str = ""       # text after ":" (for comment_only / describe)
    confidence: float = 0.0

    @property
    def has_frames(self) -> bool:
        return bool(self.frames)

    @property
    def has_status(self) -> bool:
        return self.target_status is not None


def _expand_num_list(raw: str) -> list[int]:
    result: list[int] = []
    for part in re.split(r",\s*", raw.strip()):
        part = part.strip()
        m = re.match(r"(\d+)\s*[-–—]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a <= b <= a + 500:
                result.extend(range(a, b + 1))
        elif part.isdigit():
            result.append(int(part))
    return result


def parse_message(text: str) -> ParsedEvent:
    low = text.lower()
    frames: list[int] = []

    for pat in _PATTERNS:
        for m in re.finditer(pat, low):
            groups = [g for g in m.groups() if g is not None]
            if len(groups) == 2 and groups[0].isdigit() and groups[1].isdigit():
                a, b = int(groups[0]), int(groups[1])
                if a <= b:
                    frames.extend(range(a, b + 1))
            elif groups:
                frames.extend(_expand_num_list(groups[0]))

    frames = sorted(set(frames))

    action = "move"
    if any(w in low for w in _DELETE_WORDS):
        action = "delete"
    elif any(w in low for w in _ADD_WORDS):
        action = "add"
    elif any(w in low for w in _COMMENT_WORDS):
        action = "comment_only"
    elif any(w in low for w in _DESCRIBE_WORDS):
        action = "describe"

    extra_text = ""
    if action in ("comment_only", "describe"):
        m = _INLINE_TEXT_RE.search(text)
        if m:
            extra_text = m.group(1).strip()

    status: str | None = None
    if action == "move":
        for col, aliases in STATUS_ALIASES.items():
            if any(alias in low for alias in aliases):
                status = col
                break

    confidence = (
        0.95 if (frames and (status or action != "move"))
        else 0.7 if frames
        else 0.5 if status
        else 0.0
    )
    return ParsedEvent(
        frames=frames,
        target_status=status,
        comment=text,
        action=action,
        extra_text=extra_text,
        confidence=confidence,
    )


def parse_all(text: str) -> list[ParsedEvent]:
    """Return all distinct commands found in text (≥1 item)."""
    low = text.lower()

    matched_statuses = [
        col for col, aliases in STATUS_ALIASES.items()
        if any(alias in low for alias in aliases)
    ]
    has_delete = any(w in low for w in _DELETE_WORDS)
    has_add = any(w in low for w in _ADD_WORDS)
    total_markers = len(matched_statuses) + has_delete + has_add

    if total_markers <= 1:
        return [parse_message(text)]

    # Try splitting by natural clause separators
    clauses = [c.strip() for c in re.split(r'[,;\n]|\s+и\s+', text) if c.strip()]

    if len(clauses) > 1:
        parsed = [parse_message(c) for c in clauses]

        # Inherit status from neighbour for frameless clauses
        for i, ev in enumerate(parsed):
            if ev.has_frames and not ev.has_status and ev.action == "move":
                for j in [i + 1, i - 1]:
                    if 0 <= j < len(parsed) and parsed[j].has_status:
                        ev.target_status = parsed[j].target_status
                        ev.confidence = max(ev.confidence - 0.1, 0.5)
                        break

        # Merge clauses with identical (action, status)
        merged: dict[tuple, ParsedEvent] = {}
        for ev in parsed:
            if not ev.has_frames and ev.action != "add":
                continue
            if ev.action == "move" and not ev.has_status:
                continue
            key = (ev.action, ev.target_status)
            if key in merged:
                merged[key].frames = sorted(set(merged[key].frames + ev.frames))
            else:
                merged[key] = ParsedEvent(
                    frames=list(ev.frames),
                    target_status=ev.target_status,
                    action=ev.action,
                    comment=text,
                    extra_text=ev.extra_text,
                    confidence=ev.confidence,
                )

        events = list(merged.values())
        if events:
            return events

    # No separators — assign frames to nearest status/action by position
    return _parse_by_markers(text, low)


def _parse_by_markers(text: str, low: str) -> list[ParsedEvent]:
    markers: list[tuple[int, str, str | None]] = []  # (pos, action, status)

    for col, aliases in STATUS_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            for m in re.finditer(re.escape(alias), low):
                pos = m.start()
                if not any(abs(pos - mp) < 4 for mp, _, _ in markers):
                    markers.append((pos, "move", col))

    for word in _DELETE_WORDS:
        for m in re.finditer(re.escape(word), low):
            markers.append((m.start(), "delete", None))
    for word in _ADD_WORDS:
        for m in re.finditer(re.escape(word), low):
            markers.append((m.start(), "add", None))

    markers.sort()
    if len(markers) <= 1:
        return [parse_message(text)]

    frame_occs: list[tuple[int, list[int]]] = []
    for pat in _PATTERNS:
        for m in re.finditer(pat, low):
            groups = [g for g in m.groups() if g is not None]
            frames: list[int] = []
            if len(groups) == 2 and groups[0].isdigit() and groups[1].isdigit():
                a, b = int(groups[0]), int(groups[1])
                if a <= b <= a + 500:
                    frames.extend(range(a, b + 1))
            elif groups:
                frames.extend(_expand_num_list(groups[0]))
            if frames:
                frame_occs.append((m.start(), frames))

    bucket: dict[int, list[int]] = {i: [] for i in range(len(markers))}
    for fpos, frames in frame_occs:
        nearest = min(range(len(markers)), key=lambda i: abs(markers[i][0] - fpos))
        bucket[nearest].extend(frames)

    events: list[ParsedEvent] = []
    for i, (_, action, status) in enumerate(markers):
        frames = sorted(set(bucket[i]))
        if not frames and action not in ("add",):
            continue
        events.append(ParsedEvent(
            frames=frames,
            target_status=status,
            action=action,
            comment=text,
            confidence=0.85,
        ))

    return events if events else [parse_message(text)]


def fmt_frames(frames: list[int]) -> str:
    """[1,2,3,5,6] → '1–3, 5–6'"""
    if not frames:
        return ""
    frames = sorted(set(frames))
    ranges: list[str] = []
    start = end = frames[0]
    for f in frames[1:]:
        if f == end + 1:
            end = f
        else:
            ranges.append(f"{start}–{end}" if start != end else str(start))
            start = end = f
    ranges.append(f"{start}–{end}" if start != end else str(start))
    return ", ".join(ranges)
