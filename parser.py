import re
from dataclasses import dataclass, field

# Статус → список алиасов (все в нижнем регистре)
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
    ],
    "Готово": [
        "готово", "принято", "апрув", "approved",
        "финал", "финальный", "сдано",
    ],
}

_FRAME_WORD = r"(?:кадр(?:ы|ов|а)?|шот(?:ы|ов)?|сцен(?:а|ы|у)?|frame[s]?)"
_NUM_RANGE = r"\d+\s*[-–—]\s*\d+"
_NUM_SINGLE = r"\d+"
_NUM_LIST = rf"(?:{_NUM_RANGE}|{_NUM_SINGLE})(?:\s*,\s*(?:{_NUM_RANGE}|{_NUM_SINGLE}))*"

_PATTERNS = [
    # "кадры с 1 по 18"
    rf"{_FRAME_WORD}\s+с\s+({_NUM_SINGLE})\s+по\s+({_NUM_SINGLE})",
    # "кадры 1-18" / "кадры 1, 4, 7" / "кадр 12"
    rf"{_FRAME_WORD}\s+({_NUM_LIST})",
    # "1-18 кадров"
    rf"({_NUM_LIST})\s+{_FRAME_WORD}",
    # "#к14" or "к14"
    r"#?к(\d+)\b",
]


@dataclass
class ParsedEvent:
    frames: list[int] = field(default_factory=list)
    target_status: str | None = None
    comment: str = ""
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

    status: str | None = None
    for col, aliases in STATUS_ALIASES.items():
        if any(alias in low for alias in aliases):
            status = col
            break

    confidence = 0.95 if (frames and status) else (0.7 if frames else (0.5 if status else 0.0))
    return ParsedEvent(frames=frames, target_status=status, comment=text, confidence=confidence)


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
