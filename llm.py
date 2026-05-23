import json
import logging
import re

import httpx

from parser import ParsedEvent, STATUS_ALIASES

logger = logging.getLogger(__name__)

_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_STATUSES = ", ".join(STATUS_ALIASES.keys())

_SYSTEM = f"""Ты ассистент для управления задачами Kanban-доски видеопродакшна.

Проанализируй сообщение и верни ТОЛЬКО валидный JSON:
{{
  "frames": [1, 2, 3],
  "action": "move",
  "status": "У заказчика",
  "text": ""
}}

Правила:
- "frames": целые числа — номера кадров/шотов из сообщения. [] если не упомянуты.
- "action": ровно одно из: "move" | "delete" | "add" | "comment" | "describe"
- "status": одно из доступных или null.
  Доступные: {_STATUSES}
- "text": доп. текст для "comment" или "describe", иначе ""

Никаких пояснений, только JSON."""


class LLMError(Exception):
    pass


class QwenClient:
    def __init__(self, api_key: str, model: str = "qwen-plus") -> None:
        self._key = api_key
        self._model = model
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=30,
        )

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def parse(self, text: str) -> ParsedEvent:
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
                "response_format": {"type": "json_object"},
            },
        )
        if not r.is_success:
            raise LLMError(f"Qwen {r.status_code}: {r.text[:300]}")

        raw = r.json()["choices"][0]["message"]["content"]
        return _build_event(raw, text)


def _build_event(raw: str, original: str) -> ParsedEvent:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise LLMError(f"Некорректный JSON от LLM: {e}\n{raw[:200]}")

    frames = sorted({int(f) for f in data.get("frames", []) if str(f).lstrip("-").isdigit()})

    action = data.get("action", "move")
    if action == "comment":
        action = "comment_only"
    if action not in ("move", "delete", "add", "comment_only", "describe"):
        action = "move"

    status = data.get("status") or None
    if status and status not in STATUS_ALIASES:
        status = None

    extra_text = str(data.get("text", "")).strip()

    return ParsedEvent(
        frames=frames,
        target_status=status,
        comment=original,
        action=action,
        extra_text=extra_text,
        confidence=0.9,
    )
