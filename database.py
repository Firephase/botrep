import aiosqlite
from pathlib import Path


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    project_key TEXT    NOT NULL,
                    frame       INTEGER NOT NULL,
                    task_id     TEXT    NOT NULL,
                    column_id   TEXT,
                    status      TEXT,
                    updated_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (project_key, frame)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sync_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    board_id   TEXT,
                    task_count INTEGER,
                    synced_at  TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS action_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_key TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    frames      TEXT,
                    details     TEXT,
                    actor       TEXT,
                    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    async def get_task(self, project_key: str, frame: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM tasks WHERE project_key = ? AND frame = ?",
                (project_key, frame),
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def upsert_task(
        self,
        project_key: str,
        frame: int,
        task_id: str,
        column_id: str | None = None,
        status: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO tasks (project_key, frame, task_id, column_id, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_key, frame) DO UPDATE SET
                    task_id   = excluded.task_id,
                    column_id = COALESCE(excluded.column_id, column_id),
                    status    = COALESCE(excluded.status, status),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (project_key, frame, task_id, column_id, status),
            )
            await db.commit()

    async def update_task_status(self, task_id: str, column_id: str, status: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE tasks
                SET column_id = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (column_id, status, task_id),
            )
            await db.commit()

    async def delete_task(self, project_key: str, frame: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM tasks WHERE project_key = ? AND frame = ?",
                (project_key, frame),
            )
            await db.commit()

    async def log_action(
        self,
        project_key: str,
        action: str,
        frames: list[int] | None = None,
        details: str = "",
        actor: str = "",
    ) -> None:
        frames_str = ",".join(str(f) for f in frames) if frames else ""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO action_log (project_key, action, frames, details, actor)"
                " VALUES (?, ?, ?, ?, ?)",
                (project_key, action, frames_str, details, actor),
            )
            await db.commit()

    async def get_today_actions(self, project_key: str) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM action_log
                WHERE project_key = ? AND date(created_at) = date('now')
                ORDER BY created_at
                """,
                (project_key,),
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def log_sync(self, board_id: str, task_count: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO sync_log (board_id, task_count) VALUES (?, ?)",
                (board_id, task_count),
            )
            await db.commit()
