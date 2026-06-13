import json
import logging
import re

import httpx

from parser import ParsedEvent, STATUS_ALIASES

logger = logging.getLogger(__name__)

_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_STATUSES = ", ".join(STATUS_ALIASES.keys())

_SYSTEM = f"""Ты ассистент для управления задачами Kanban-доски видеопродакшна.

Проанализируй сообщение и извлеки ВСЕ команды. Верни ТОЛЬКО валидный JSON — массив:

[
  {{"frames": [1, 2, 3], "action": "move",    "status": "У заказчика", "text": ""}},
  {{"frames": [5],        "action": "delete",  "status": null,          "text": ""}},
  {{"frames": [7],        "action": "describe","status": null,          "text": "Новое описание"}}
]

Правила:
- Найди ВСЕ команды — их может быть много.
- "frames": массив чисел. Диапазон "1-6" → [1,2,3,4,5,6]. [] если кадры не нужны.
- "action": строго одно из: "move" | "delete" | "add" | "comment" | "describe"
- "status": точное название колонки из списка или null.
  Доступные колонки: {_STATUSES}
- "text": текст для "comment"/"describe", иначе "".
- Для "add" без явного кадра укажи frames: [].
- Только JSON-массив, никаких пояснений."""


class LLMError(Exception):
    pass


class QwenClient:
    def __init__(self, api_key: str, model: str = "qwen-plus", proxy: str = "") -> None:
        self._key = api_key
        self._model = model
        self._proxy = proxy
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=30,
            **({"proxy": self._proxy} if self._proxy else {}),
        )

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def parse_all(self, text: str) -> list[ParsedEvent]:
        assert self._http, "Client not started"
        r = await self._http.post(
            "/chat/completions",
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.1,
            },
        )
        if not r.is_success:
            raise LLMError(f"Qwen {r.status_code}: {r.text[:300]}")

        raw = r.json()["choices"][0]["message"]["content"]
        return _build_events(raw, text)

    # backward-compat alias
    async def parse(self, text: str) -> ParsedEvent:
        events = await self.parse_all(text)
        return events[0] if events else ParsedEvent()


def _build_events(raw: str, original: str) -> list[ParsedEvent]:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise LLMError(f"Некорректный JSON от LLM: {e}\n{raw[:200]}")

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise LLMError(f"Ожидался массив, получен {type(data).__name__}")

    events: list[ParsedEvent] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        frames = sorted({int(f) for f in item.get("frames", []) if str(f).lstrip("-").isdigit()})
        action = item.get("action", "move")
        if action == "comment":
            action = "comment_only"
        if action not in ("move", "delete", "add", "comment_only", "describe"):
            action = "move"
        status = item.get("status") or None
        if status and status not in STATUS_ALIASES:
            status = None
        extra_text = str(item.get("text", "")).strip()
        events.append(ParsedEvent(
            frames=frames,
            target_status=status,
            comment=original,
            action=action,
            extra_text=extra_text,
            confidence=0.9,
        ))

    return events
