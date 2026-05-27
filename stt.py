import io
import logging

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.groq.com/openai/v1"


class STTError(Exception):
    pass


class GroqSTT:
    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo") -> None:
        self._key = api_key
        self._model = model
        self._http: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=60,
        )

    async def stop(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def transcribe(self, audio_bytes: bytes, filename: str = "voice.ogg") -> str:
        assert self._http, "Client not started"
        r = await self._http.post(
            "/audio/transcriptions",
            files={"file": (filename, audio_bytes, "audio/ogg")},
            data={"model": self._model, "language": "ru", "response_format": "json"},
        )
        if not r.is_success:
            raise STTError(f"Groq {r.status_code}: {r.text[:300]}")
        return r.json().get("text", "").strip()
