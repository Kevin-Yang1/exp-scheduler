from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import threading


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL,
                    cwd TEXT,
                    env TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    queue_rank INTEGER,
                    assigned_gpu INTEGER,
                    pid INTEGER,
                    exit_code INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    log_path TEXT,
                    notes TEXT
                )
                """
            )
            self._ensure_columns(
                conn,
                "tasks",
                {
                    "profile_id": "INTEGER",
                    "profile_name": "TEXT",
                    "shell_setup": "TEXT",
                    "attempt_count": "INTEGER",
                    "next_retry_at": "TEXT",
                    "requested_gpu": "INTEGER",
                },
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS environment_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    cwd TEXT,
                    env TEXT NOT NULL DEFAULT '{}',
                    shell_setup TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('queue_paused', '0')"
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_task(
        self,
        *,
        name: str,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        notes: str | None,
        requested_gpu: int | None = None,
        profile_id: int | None = None,
        profile_name: str | None = None,
        shell_setup: str | None = None,
    ) -> dict[str, object]:
        with self._lock, self._connect() as conn:
            queue_rank = self._next_queue_rank(conn)
            now = utc_now_iso()
            cursor = conn.execute(
                """
                INSERT INTO tasks(
                    name, command, cwd, env, status, queue_rank, assigned_gpu, pid,
                    exit_code, created_at, started_at, finished_at, log_path, notes,
                    profile_id, profile_name, shell_setup, attempt_count, next_retry_at,
                    requested_gpu
                ) VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL, NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?, 0, NULL, ?)
                """,
                (
                    name,
                    command,
                    cwd,
                    json.dumps(env),
                    queue_rank,
                    now,
                    notes,
                    profile_id,
                    profile_name,
                    shell_setup,
                    requested_gpu,
                ),
            )
            conn.commit()
            return self.get_task(cursor.lastrowid)

    def list_profiles(self) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM environment_profiles
                ORDER BY updated_at DESC, id DESC
                """
            ).fetchall()
        return [self._row_to_profile(row) for row in rows]

    def get_profile(self, profile_id: int) -> dict[str, object] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM environment_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_profile(row)

    def create_profile(
        self,
        *,
        name: str,
        cwd: str | None,
        env: dict[str, str],
        shell_setup: str | None,
        notes: str | None,
    ) -> dict[str, object]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO environment_profiles(
                        name, cwd, env, shell_setup, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        cwd,
                        json.dumps(env),
                        shell_setup,
                        notes,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"环境配置名称已存在: {name}") from exc
            conn.commit()
        profile = self.get_profile(cursor.lastrowid)
        if profile is None:
            raise ValueError("环境配置创建失败")
        return profile

    def update_profile(
        self,
        profile_id: int,
        *,
        name: str,
        cwd: str | None,
        env: dict[str, str],
        shell_setup: str | None,
        notes: str | None,
    ) -> dict[str, object]:
        with self._lock, self._connect() as conn:
            try:
                cursor = conn.execute(
                    """
                    UPDATE environment_profiles
                    SET name = ?,
                        cwd = ?,
                        env = ?,
                        shell_setup = ?,
                        notes = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        cwd,
                        json.dumps(env),
                        shell_setup,
                        notes,
                        utc_now_iso(),
                        profile_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"环境配置名称已存在: {name}") from exc
            conn.commit()
        if cursor.rowcount == 0:
            raise ValueError("环境配置不存在")
        profile = self.get_profile(profile_id)
        if profile is None:
            raise ValueError("环境配置不存在")
        return profile

    def delete_profile(self, profile_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM environment_profiles WHERE id = ?",
                (profile_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_task(self, task_id: int) -> dict[str, object] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def list_tasks(self, history_limit: int = 100) -> dict[str, object]:
        with self._lock, self._connect() as conn:
            queued = conn.execute(
                "SELECT * FROM tasks WHERE status = 'queued' ORDER BY queue_rank ASC, id ASC"
            ).fetchall()
            running = conn.execute(
                "SELECT * FROM tasks WHERE status = 'running' ORDER BY started_at ASC, id ASC"
            ).fetchall()
            history = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status NOT IN ('queued', 'running')
                ORDER BY COALESCE(finished_at, created_at) DESC, id DESC
                LIMIT ?
                """,
                (history_limit,),
            ).fetchall()
            paused_value = conn.execute(
                "SELECT value FROM meta WHERE key = 'queue_paused'"
            ).fetchone()
        return {
            "queued": [self._row_to_task(row) for row in queued],
            "running": [self._row_to_task(row) for row in running],
            "history": [self._row_to_task(row) for row in history],
            "queue_paused": paused_value is not None and paused_value["value"] == "1",
        }

    def list_queued_tasks(self) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'queued' ORDER BY queue_rank ASC, id ASC"
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_running_tasks(self) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'running' ORDER BY started_at ASC, id ASC"
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def delete_queued_task(self, task_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM tasks WHERE id = ? AND status = 'queued'",
                (task_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def reorder_queue(self, task_ids: Sequence[int]) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE status = 'queued' ORDER BY queue_rank ASC, id ASC"
            ).fetchall()
            current_ids = [row["id"] for row in rows]
            if current_ids != list(task_ids):
                if set(current_ids) != set(task_ids) or len(current_ids) != len(task_ids):
                    raise ValueError("重排请求必须包含完整的排队任务列表")
            for idx, task_id in enumerate(task_ids, start=1):
                conn.execute(
                    "UPDATE tasks SET queue_rank = ? WHERE id = ? AND status = 'queued'",
                    (idx, task_id),
                )
            conn.commit()
        return self.list_queued_tasks()

    def get_queue_paused(self) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'queue_paused'"
            ).fetchone()
        return row is not None and row["value"] == "1"

    def set_queue_paused(self, paused: bool) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES('queue_paused', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("1" if paused else "0",),
            )
            conn.commit()
        return paused

    def get_allowed_gpu_ids(self) -> list[int] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'allowed_gpu_ids'"
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return None
        if payload is None:
            return None
        if not isinstance(payload, list):
            return None
        return [int(item) for item in payload]

    def set_allowed_gpu_ids(self, allowed_gpu_ids: list[int] | None) -> list[int] | None:
        with self._lock, self._connect() as conn:
            if allowed_gpu_ids is None:
                conn.execute("DELETE FROM meta WHERE key = 'allowed_gpu_ids'")
            else:
                conn.execute(
                    """
                    INSERT INTO meta(key, value) VALUES('allowed_gpu_ids', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (json.dumps(allowed_gpu_ids),),
                )
            conn.commit()
        return allowed_gpu_ids

    def mark_task_running(
        self,
        *,
        task_id: int,
        gpu_id: int,
        pid: int,
        log_path: str,
    ) -> dict[str, object]:
        started_at = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    queue_rank = NULL,
                    assigned_gpu = ?,
                    pid = ?,
                    started_at = ?,
                    log_path = ?,
                    finished_at = NULL,
                    exit_code = NULL,
                    next_retry_at = NULL,
                    attempt_count = COALESCE(attempt_count, 0) + 1
                WHERE id = ? AND status = 'queued'
                """,
                (gpu_id, pid, started_at, log_path, task_id),
            )
            conn.commit()
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def finish_task(
        self,
        *,
        task_id: int,
        status: str,
        exit_code: int | None,
        pid: int | None = None,
    ) -> dict[str, object]:
        finished_at = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    exit_code = ?,
                    finished_at = ?,
                    pid = ?
                WHERE id = ?
                """,
                (status, exit_code, finished_at, pid, task_id),
            )
            conn.commit()
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def mark_task_launch_failed(
        self, *, task_id: int, log_path: str, message: str
    ) -> dict[str, object]:
        finished_at = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    queue_rank = NULL,
                    finished_at = ?,
                    exit_code = -1,
                    log_path = ?,
                    notes = CASE
                        WHEN notes IS NULL OR notes = '' THEN ?
                        ELSE notes || CHAR(10) || ?
                    END
                WHERE id = ?
                """,
                (finished_at, log_path, message, message, task_id),
            )
            conn.commit()
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def schedule_task_retry(
        self,
        *,
        task_id: int,
        next_retry_at: str,
        exit_code: int | None,
    ) -> dict[str, object]:
        with self._lock, self._connect() as conn:
            queue_rank = self._head_queue_rank(conn)
            conn.execute(
                """
                UPDATE tasks
                SET status = 'queued',
                    queue_rank = ?,
                    assigned_gpu = NULL,
                    pid = NULL,
                    exit_code = ?,
                    finished_at = ?,
                    next_retry_at = ?
                WHERE id = ?
                """,
                (queue_rank, exit_code, utc_now_iso(), next_retry_at, task_id),
            )
            conn.commit()
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def clone_task_for_requeue(self, task_id: int) -> dict[str, object]:
        task = self.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        if task["status"] not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("只有失败、取消或中断的任务可以重新入队")
        return self.create_task(
            name=str(task["name"]),
            command=str(task["command"]),
            cwd=task["cwd"] if isinstance(task["cwd"], str) else None,
            env=dict(task["env"]),
            notes=task["notes"] if isinstance(task["notes"], str) else None,
            requested_gpu=task["requested_gpu"]
            if isinstance(task["requested_gpu"], int)
            else None,
            profile_id=task["profile_id"] if isinstance(task["profile_id"], int) else None,
            profile_name=task["profile_name"]
            if isinstance(task["profile_name"], str)
            else None,
            shell_setup=task["shell_setup"]
            if isinstance(task["shell_setup"], str)
            else None,
        )

    def mark_running_tasks_interrupted(self) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'interrupted',
                    finished_at = ?,
                    pid = NULL
                WHERE status = 'running'
                """,
                (utc_now_iso(),),
            )
            conn.commit()
            return cursor.rowcount

    def _next_queue_rank(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(queue_rank), 0) AS max_rank FROM tasks WHERE status = 'queued'"
        ).fetchone()
        return int(row["max_rank"]) + 1

    def _head_queue_rank(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT MIN(queue_rank) AS min_rank FROM tasks WHERE status = 'queued'"
        ).fetchone()
        min_rank = row["min_rank"]
        if min_rank is None:
            return 1
        return int(min_rank) - 1

    def _ensure_columns(
        self,
        conn: sqlite3.Connection,
        table_name: str,
        expected_columns: dict[str, str],
    ) -> None:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing_columns = {row["name"] for row in rows}
        for column_name, column_type in expected_columns.items():
            if column_name in existing_columns:
                continue
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            )

    def _row_to_task(self, row: sqlite3.Row) -> dict[str, object]:
        env_raw = row["env"] or "{}"
        keys = set(row.keys())
        return {
            "id": row["id"],
            "name": row["name"],
            "command": row["command"],
            "cwd": row["cwd"],
            "env": json.loads(env_raw),
            "status": row["status"],
            "queue_rank": row["queue_rank"],
            "assigned_gpu": row["assigned_gpu"],
            "pid": row["pid"],
            "exit_code": row["exit_code"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "log_path": row["log_path"],
            "notes": row["notes"],
            "profile_id": row["profile_id"] if "profile_id" in keys else None,
            "profile_name": row["profile_name"] if "profile_name" in keys else None,
            "shell_setup": row["shell_setup"] if "shell_setup" in keys else None,
            "attempt_count": row["attempt_count"] if "attempt_count" in keys and row["attempt_count"] is not None else 0,
            "next_retry_at": row["next_retry_at"] if "next_retry_at" in keys else None,
            "requested_gpu": row["requested_gpu"] if "requested_gpu" in keys else None,
        }

    def _row_to_profile(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "name": row["name"],
            "cwd": row["cwd"],
            "env": json.loads(row["env"] or "{}"),
            "shell_setup": row["shell_setup"],
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
