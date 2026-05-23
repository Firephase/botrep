import logging
import re
from dataclasses import dataclass, field

from database import Database
from parser import ParsedEvent, fmt_frames
from yougile import YouGileClient, YouGileError

logger = logging.getLogger(__name__)

_FRAME_RE = re.compile(
    r"[Кк]адр\s*(\d+)|[Шш]от\s*(\d+)|[Сс]цена?\s*(\d+)",
    re.IGNORECASE,
)


def _frame_from_title(title: str) -> int | None:
    m = _FRAME_RE.search(title)
    if m:
        return int(next(g for g in m.groups() if g is not None))
    return None


@dataclass
class SyncResult:
    updated: list[int] = field(default_factory=list)
    not_found: list[int] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)

    def summary(self) -> str:
        parts: list[str] = []
        if self.updated:
            parts.append(f"Обновлено {len(self.updated)}: {fmt_frames(self.updated)}")
        if self.not_found:
            parts.append(f"Не найдено в YouGile: {fmt_frames(self.not_found)}")
        if self.errors:
            errs = fmt_frames(list(self.errors))
            first_err = next(iter(self.errors.values()))
            parts.append(f"Ошибки ({errs}): {first_err}")
        return "\n".join(parts) or "Ничего не изменено"


class SyncEngine:
    def __init__(
        self,
        yougile: YouGileClient,
        db: Database,
        board_id: str,
        project_key: str,
    ) -> None:
        self._yougile = yougile
        self._db = db
        self._board_id = board_id
        self._project_key = project_key
        self._columns: dict[str, str] = {}  # column_name → column_id

    # ── board sync ─────────────────────────────────────────────────────────

    async def sync_board(self) -> int:
        cols = await self._yougile.get_columns(self._board_id)
        self._columns = {c["title"]: c["id"] for c in cols}
        logger.info("Колонки на доске: %s", list(self._columns))

        tasks = await self._yougile.get_tasks(self._board_id)
        logger.info("Задач на доске: %d", len(tasks))

        synced = 0
        for task in tasks:
            frame = _frame_from_title(task.get("name") or task.get("title", ""))
            if frame is None:
                continue
            col_id = task.get("columnId", "")
            col_name = next((n for n, i in self._columns.items() if i == col_id), "")
            await self._db.upsert_task(
                project_key=self._project_key,
                frame=frame,
                task_id=task["id"],
                column_id=col_id,
                status=col_name,
            )
            synced += 1

        await self._db.log_sync(self._board_id, synced)
        return synced

    # ── event processing ───────────────────────────────────────────────────

    async def process_event(self, event: ParsedEvent) -> SyncResult:
        result = SyncResult()
        if not (event.has_frames and event.has_status):
            return result

        col_id = self._columns.get(event.target_status)
        if not col_id:
            logger.warning(
                "Колонка '%s' не найдена. Доступные: %s",
                event.target_status,
                list(self._columns),
            )
            result.errors[-1] = (
                f"Колонка «{event.target_status}» не найдена. "
                f"Доступные: {', '.join(self._columns)}"
            )
            return result

        for frame in event.frames:
            row = await self._db.get_task(self._project_key, frame)
            if not row:
                row = await self._search_and_cache(frame)

            if not row:
                result.not_found.append(frame)
                continue

            try:
                await self._yougile.move_task(row["task_id"], col_id)
                await self._yougile.add_comment(row["task_id"], event.comment)
                await self._db.update_task_status(row["task_id"], col_id, event.target_status)
                result.updated.append(frame)
            except YouGileError as e:
                logger.error("Кадр %d (task %s): %s", frame, row["task_id"], e)
                result.errors[frame] = str(e)

        return result

    async def _search_and_cache(self, frame: int) -> dict | None:
        tasks = await self._yougile.get_tasks(self._board_id)
        for task in tasks:
            if _frame_from_title(task.get("name") or task.get("title", "")) == frame:
                await self._db.upsert_task(self._project_key, frame, task["id"])
                return await self._db.get_task(self._project_key, frame)
        return None

    def get_column_names(self) -> list[str]:
        return list(self._columns)
