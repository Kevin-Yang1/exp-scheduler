from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import threading


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


NORMAL_QUEUE = "normal"
URGENT_QUEUE = "urgent"
VALID_QUEUE_NAMES = {NORMAL_QUEUE, URGENT_QUEUE}
SCHEDULER_SETTINGS_META_KEY = "scheduler_settings"


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
                    "gpu_memory_budget_mb": "INTEGER",
                    "queue_name": "TEXT",
                },
            )
            conn.execute(
                """
                UPDATE tasks
                SET queue_name = ?
                WHERE queue_name IS NULL OR queue_name = ''
                """,
                (NORMAL_QUEUE,),
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
        gpu_memory_budget_mb: int | None = None,
        queue_name: str = NORMAL_QUEUE,
        profile_id: int | None = None,
        profile_name: str | None = None,
        shell_setup: str | None = None,
    ) -> dict[str, object]:
        normalized_queue_name = self._normalize_queue_name(queue_name)
        with self._lock, self._connect() as conn:
            queue_rank = self._next_queue_rank(conn, normalized_queue_name)
            now = utc_now_iso()
            cursor = conn.execute(
                """
                INSERT INTO tasks(
                    name, command, cwd, env, status, queue_rank, assigned_gpu, pid,
                    exit_code, created_at, started_at, finished_at, log_path, notes,
                    profile_id, profile_name, shell_setup, attempt_count, next_retry_at,
                    requested_gpu, gpu_memory_budget_mb, queue_name
                ) VALUES (?, ?, ?, ?, 'queued', ?, NULL, NULL, NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?, 0, NULL, ?, ?, ?)
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
                    gpu_memory_budget_mb,
                    normalized_queue_name,
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
                """
                SELECT * FROM tasks
                WHERE status = 'queued' AND queue_name = ?
                ORDER BY queue_rank ASC, id ASC
                """,
                (NORMAL_QUEUE,),
            ).fetchall()
            urgent_queued = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'queued' AND queue_name = ?
                ORDER BY queue_rank ASC, id ASC
                """,
                (URGENT_QUEUE,),
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
            "urgent_queued": [self._row_to_task(row) for row in urgent_queued],
            "running": [self._row_to_task(row) for row in running],
            "history": [self._row_to_task(row) for row in history],
            "queue_paused": paused_value is not None and paused_value["value"] == "1",
        }

    def list_queued_tasks(self) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'queued'
                ORDER BY CASE queue_name
                    WHEN ? THEN 0
                    ELSE 1
                END ASC, queue_rank ASC, id ASC
                """,
                (URGENT_QUEUE,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_queue_tasks(self, queue_name: str = NORMAL_QUEUE) -> list[dict[str, object]]:
        normalized_queue_name = self._normalize_queue_name(queue_name)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'queued' AND queue_name = ?
                ORDER BY queue_rank ASC, id ASC
                """,
                (normalized_queue_name,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_running_tasks(self) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'running' ORDER BY started_at ASC, id ASC"
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def delete_task(self, task_id: int) -> dict[str, object] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "running":
                raise ValueError("运行中的任务不能删除")
            conn.execute(
                "DELETE FROM tasks WHERE id = ?",
                (task_id,),
            )
            conn.commit()
        return self._row_to_task(row)

    def update_queued_task(
        self,
        task_id: int,
        *,
        name: str,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        notes: str | None,
        requested_gpu: int | None = None,
        gpu_memory_budget_mb: int | None = None,
        queue_name: str = NORMAL_QUEUE,
        profile_id: int | None = None,
        profile_name: str | None = None,
        shell_setup: str | None = None,
    ) -> dict[str, object]:
        normalized_queue_name = self._normalize_queue_name(queue_name)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT queue_rank, queue_name
                FROM tasks
                WHERE id = ? AND status = 'queued'
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError("只能修改排队中的任务")
            current_queue_name = self._normalize_queue_name(row["queue_name"])
            if current_queue_name == normalized_queue_name and row["queue_rank"] is not None:
                queue_rank = int(row["queue_rank"])
            else:
                queue_rank = self._next_queue_rank(conn, normalized_queue_name)
            conn.execute(
                """
                UPDATE tasks
                SET name = ?,
                    command = ?,
                    cwd = ?,
                    env = ?,
                    notes = ?,
                    requested_gpu = ?,
                    gpu_memory_budget_mb = ?,
                    queue_name = ?,
                    queue_rank = ?,
                    profile_id = ?,
                    profile_name = ?,
                    shell_setup = ?,
                    exit_code = NULL,
                    finished_at = NULL,
                    next_retry_at = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (
                    name,
                    command,
                    cwd,
                    json.dumps(env),
                    notes,
                    requested_gpu,
                    gpu_memory_budget_mb,
                    normalized_queue_name,
                    queue_rank,
                    profile_id,
                    profile_name,
                    shell_setup,
                    task_id,
                ),
            )
            conn.commit()
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def reorder_queue(
        self,
        task_ids: Sequence[int],
        *,
        queue_name: str = NORMAL_QUEUE,
    ) -> list[dict[str, object]]:
        normalized_queue_name = self._normalize_queue_name(queue_name)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM tasks
                WHERE status = 'queued' AND queue_name = ?
                ORDER BY queue_rank ASC, id ASC
                """,
                (normalized_queue_name,),
            ).fetchall()
            current_ids = [row["id"] for row in rows]
            if current_ids != list(task_ids):
                if set(current_ids) != set(task_ids) or len(current_ids) != len(task_ids):
                    raise ValueError("重排请求必须包含完整的排队任务列表")
            for idx, task_id in enumerate(task_ids, start=1):
                conn.execute(
                    """
                    UPDATE tasks
                    SET queue_rank = ?
                    WHERE id = ? AND status = 'queued' AND queue_name = ?
                    """,
                    (idx, task_id, normalized_queue_name),
                )
            conn.commit()
        return self.list_queue_tasks(normalized_queue_name)

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

    def get_scheduler_settings(self) -> dict[str, object] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (SCHEDULER_SETTINGS_META_KEY,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def set_scheduler_settings(
        self,
        *,
        poll_interval_seconds: float,
        gpu_idle_required_checks: int,
    ) -> dict[str, object]:
        settings: dict[str, object] = {
            "poll_interval_seconds": poll_interval_seconds,
            "gpu_idle_required_checks": gpu_idle_required_checks,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (SCHEDULER_SETTINGS_META_KEY, json.dumps(settings)),
            )
            conn.commit()
        return settings

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


    def get_gpu_schedule(self) -> dict[str, dict[str, str | int]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'gpu_schedule'"
            ).fetchone()
        if row is None:
            return {}
        try:
            payload = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        schedule: dict[str, dict[str, str | int]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            action = value.get("action")
            run_at = value.get("run_at")
            if action not in {"enable", "disable"} or not isinstance(run_at, str):
                continue
            try:
                gpu_id = int(key)
            except (TypeError, ValueError):
                continue
            schedule[str(gpu_id)] = {"action": str(action), "run_at": run_at}
        return schedule

    def set_gpu_schedule(
        self,
        schedule: dict[str, dict[str, str | int]],
    ) -> dict[str, dict[str, str | int]]:
        with self._lock, self._connect() as conn:
            if schedule:
                conn.execute(
                    """
                    INSERT INTO meta(key, value) VALUES('gpu_schedule', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (json.dumps(schedule),),
                )
            else:
                conn.execute("DELETE FROM meta WHERE key = 'gpu_schedule'")
            conn.commit()
        return schedule

    def set_gpu_schedule_entry(
        self,
        gpu_id: int,
        *,
        action: str,
        run_at: str,
    ) -> dict[str, dict[str, str | int]]:
        if action not in {"enable", "disable"}:
            raise ValueError("GPU 定时动作无效")
        schedule = self.get_gpu_schedule()
        schedule[str(int(gpu_id))] = {"action": action, "run_at": run_at}
        return self.set_gpu_schedule(schedule)

    def clear_gpu_schedule_entry(self, gpu_id: int) -> dict[str, dict[str, str | int]]:
        schedule = self.get_gpu_schedule()
        schedule.pop(str(int(gpu_id)), None)
        return self.set_gpu_schedule(schedule)

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
            queue_name = self._queue_name_for_task(conn, task_id)
            queue_rank = self._head_queue_rank(conn, queue_name)
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

    def preempt_running_task_to_queue_head(
        self,
        task_id: int,
        *,
        queue_name: str = NORMAL_QUEUE,
    ) -> dict[str, object]:
        normalized_queue_name = self._normalize_queue_name(queue_name)
        with self._lock, self._connect() as conn:
            queue_rank = self._head_queue_rank(conn, normalized_queue_name)
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'queued',
                    queue_rank = ?,
                    assigned_gpu = NULL,
                    pid = NULL,
                    exit_code = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    next_retry_at = NULL,
                    queue_name = ?
                WHERE id = ? AND status = 'running'
                """,
                (queue_rank, normalized_queue_name, task_id),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise ValueError("只有运行中的任务可以抢占后回队列")
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def requeue_running_task_to_queue_head(
        self,
        task_id: int,
        *,
        exit_code: int | None,
    ) -> dict[str, object]:
        with self._lock, self._connect() as conn:
            queue_name = self._queue_name_for_task(conn, task_id)
            queue_rank = self._head_queue_rank(conn, queue_name)
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = 'queued',
                    queue_rank = ?,
                    assigned_gpu = NULL,
                    pid = NULL,
                    exit_code = ?,
                    started_at = NULL,
                    finished_at = NULL,
                    next_retry_at = NULL
                WHERE id = ? AND status = 'running'
                """,
                (queue_rank, exit_code, task_id),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise ValueError("只有运行中的任务可以中断后回队列")
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def requeue_running_tasks_to_queue_head(self) -> int:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, queue_name
                FROM tasks
                WHERE status = 'running'
                ORDER BY COALESCE(started_at, created_at) ASC, id ASC
                """
            ).fetchall()
            if not rows:
                return 0

            rows_by_queue: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                queue_name = self._normalize_queue_name(row["queue_name"])
                rows_by_queue.setdefault(queue_name, []).append(row)

            for queue_name, queue_rows in rows_by_queue.items():
                min_row = conn.execute(
                    """
                    SELECT MIN(queue_rank) AS min_rank
                    FROM tasks
                    WHERE status = 'queued' AND queue_name = ?
                    """,
                    (queue_name,),
                ).fetchone()
                min_rank = min_row["min_rank"]
                base_rank = 1 if min_rank is None else int(min_rank) - len(queue_rows)
                for offset, row in enumerate(queue_rows):
                    conn.execute(
                        """
                        UPDATE tasks
                        SET status = 'queued',
                            queue_rank = ?,
                            assigned_gpu = NULL,
                            pid = NULL,
                            started_at = NULL,
                            finished_at = NULL,
                            next_retry_at = NULL,
                            queue_name = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (base_rank + offset, queue_name, row["id"]),
                    )
            conn.commit()
            return len(rows)

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
            gpu_memory_budget_mb=task["gpu_memory_budget_mb"]
            if isinstance(task.get("gpu_memory_budget_mb"), int)
            else None,
            queue_name=str(task.get("queue_name") or NORMAL_QUEUE),
            profile_id=task["profile_id"] if isinstance(task["profile_id"], int) else None,
            profile_name=task["profile_name"]
            if isinstance(task["profile_name"], str)
            else None,
            shell_setup=task["shell_setup"]
            if isinstance(task["shell_setup"], str)
            else None,
        )

    def _next_queue_rank(self, conn: sqlite3.Connection, queue_name: str) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(queue_rank), 0) AS max_rank
            FROM tasks
            WHERE status = 'queued' AND queue_name = ?
            """,
            (self._normalize_queue_name(queue_name),),
        ).fetchone()
        return int(row["max_rank"]) + 1

    def _head_queue_rank(self, conn: sqlite3.Connection, queue_name: str) -> int:
        row = conn.execute(
            """
            SELECT MIN(queue_rank) AS min_rank
            FROM tasks
            WHERE status = 'queued' AND queue_name = ?
            """,
            (self._normalize_queue_name(queue_name),),
        ).fetchone()
        min_rank = row["min_rank"]
        if min_rank is None:
            return 1
        return int(min_rank) - 1

    def _queue_name_for_task(self, conn: sqlite3.Connection, task_id: int) -> str:
        row = conn.execute(
            "SELECT queue_name FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"任务不存在: {task_id}")
        return self._normalize_queue_name(row["queue_name"])

    def _normalize_queue_name(self, queue_name: str | None) -> str:
        value = str(queue_name or NORMAL_QUEUE).strip().lower()
        if value not in VALID_QUEUE_NAMES:
            raise ValueError(f"未知队列类型: {value}")
        return value

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
            "gpu_memory_budget_mb": (
                row["gpu_memory_budget_mb"]
                if "gpu_memory_budget_mb" in keys and row["gpu_memory_budget_mb"] is not None
                else None
            ),
            "queue_name": (
                self._normalize_queue_name(row["queue_name"])
                if "queue_name" in keys
                else NORMAL_QUEUE
            ),
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
