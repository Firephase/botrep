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
