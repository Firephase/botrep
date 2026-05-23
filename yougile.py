import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)
_BASE = "https://ru.yougile.com/api-v2"


class YouGileError(Exception):
    pass


class YouGileClient:
    def __init__(self, api_key: str) -> None:
        self._key = api_key
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

    # ── low-level ──────────────────────────────────────────────────────────

    async def _get(self, path: str, **params: Any) -> Any:
        assert self._http, "Client not started"
        filtered = {k: v for k, v in params.items() if v is not None}
        r = await self._http.get(path, params=filtered)
        if not r.is_success:
            raise YouGileError(f"GET {path} → {r.status_code}: {r.text[:400]}")
        return r.json()

    async def _put(self, path: str, body: dict) -> Any:
        assert self._http
        r = await self._http.put(path, json=body)
        if not r.is_success:
            raise YouGileError(f"PUT {path} → {r.status_code}: {r.text[:400]}")
        return r.json()

    async def _post(self, path: str, body: dict) -> Any:
        assert self._http
        r = await self._http.post(path, json=body)
        if not r.is_success:
            raise YouGileError(f"POST {path} → {r.status_code}: {r.text[:400]}")
        return r.json()

    async def _delete(self, path: str) -> Any:
        assert self._http
        r = await self._http.delete(path)
        if not r.is_success:
            raise YouGileError(f"DELETE {path} → {r.status_code}: {r.text[:400]}")
        try:
            return r.json()
        except Exception:
            return {}

    @staticmethod
    def _list(data: Any) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("content", "data", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    # ── public API ─────────────────────────────────────────────────────────

    async def test_connection(self) -> str:
        try:
            data = await self._get("/company")
            name = data.get("title") or data.get("name") or "OK"
            return f"компания: {name}"
        except YouGileError:
            projs = await self.get_projects()
            return f"проектов доступно: {len(projs)}"

    async def get_projects(self) -> list[dict]:
        return self._list(await self._get("/projects"))

    async def get_boards(self, project_id: str) -> list[dict]:
        return self._list(await self._get("/boards", projectId=project_id))

    async def get_columns(self, board_id: str) -> list[dict]:
        return self._list(await self._get("/columns", boardId=board_id))

    async def get_tasks(self, board_id: str) -> list[dict]:
        cols = await self.get_columns(board_id)
        all_tasks: list[dict] = []
        for col in cols:
            try:
                batch = self._list(await self._get("/tasks", columnId=col["id"]))
                all_tasks.extend(batch)
            except YouGileError as e:
                logger.warning("Колонка %s: %s", col.get("title"), e)
        return all_tasks

    async def get_users(self) -> list[dict]:
        for path in ("/employees", "/users", "/company/employees"):
            try:
                users = self._list(await self._get(path))
                if users:
                    return users
            except YouGileError:
                continue
        return []

    async def move_task(self, task_id: str, column_id: str) -> dict:
        return await self._put(f"/tasks/{task_id}", {"columnId": column_id})

    async def update_task(self, task_id: str, **fields: Any) -> dict:
        return await self._put(f"/tasks/{task_id}", dict(fields))

    async def create_task(self, column_id: str, title: str) -> dict:
        return await self._post("/tasks", {"title": title, "columnId": column_id})

    async def delete_task(self, task_id: str) -> None:
        await self._delete(f"/tasks/{task_id}")

    async def add_comment(self, task_id: str, text: str) -> dict:
        return await self._post(f"/chats/{task_id}/messages", {"text": text})
