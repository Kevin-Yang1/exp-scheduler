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
STAGED_QUEUE = "staged"
VALID_QUEUE_NAMES = {NORMAL_QUEUE, URGENT_QUEUE, STAGED_QUEUE}
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
                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task_id INTEGER NOT NULL,
                    depends_on_task_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, depends_on_task_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_attempts (
                    task_id INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT,
                    exit_code INTEGER,
                    log_path TEXT,
                    PRIMARY KEY(task_id, attempt)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_task_attempts_task
                ON task_attempts(task_id, attempt)
                """
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
                """
                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id INTEGER,
                    title TEXT NOT NULL,
                    detail TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_operation_logs_created_at
                ON operation_logs(created_at DESC, id DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_operation_logs_entity
                ON operation_logs(entity_type, entity_id)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_gpu_leases (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    gpu_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    released_at TEXT,
                    notes TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_gpu_leases_active
                ON agent_gpu_leases(released_at, expires_at)
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
        depends_on_ids: Sequence[int] | None = None,
    ) -> dict[str, object]:
        normalized_queue_name = self._normalize_queue_name(queue_name)
        task_status = self._status_for_queue(normalized_queue_name)
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
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?, 0, NULL, ?, ?, ?)
                """,
                (
                    name,
                    command,
                    cwd,
                    json.dumps(env),
                    task_status,
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
            task_id = int(cursor.lastrowid)
            if depends_on_ids is not None:
                self._replace_dependencies(conn, task_id, depends_on_ids)
            conn.commit()
            return self.get_task(task_id)

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

    def add_operation_log(
        self,
        *,
        level: str,
        source: str,
        action: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        title: str,
        detail: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO operation_logs(
                    created_at, level, source, action, entity_type, entity_id,
                    title, detail, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    level,
                    source,
                    action,
                    entity_type,
                    entity_id,
                    title,
                    detail,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        log = self.get_operation_log(int(cursor.lastrowid))
        if log is None:
            raise ValueError("操作日志写入失败")
        return log

    def get_operation_log(self, log_id: int) -> dict[str, object] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operation_logs WHERE id = ?",
                (log_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_operation_log(row)

    def list_operation_logs(
        self,
        *,
        limit: int = 200,
        level: str | None = None,
        source: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if level:
            clauses.append("level = ?")
            params.append(level)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if action:
            clauses.append("action = ?")
            params.append(action)
        if entity_type:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if query:
            like_query = f"%{query}%"
            clauses.append(
                """
                (
                    title LIKE ?
                    OR detail LIKE ?
                    OR action LIKE ?
                    OR source LIKE ?
                    OR entity_type LIKE ?
                    OR CAST(entity_id AS TEXT) LIKE ?
                    OR metadata LIKE ?
                )
                """
            )
            params.extend([like_query] * 7)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        safe_limit = max(1, min(int(limit), 1000))
        params.append(safe_limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM operation_logs
                {where_sql}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_operation_log(row) for row in rows]

    def clear_operation_logs(self) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM operation_logs")
            conn.commit()
            return cursor.rowcount

    def get_task(self, task_id: int) -> dict[str, object] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    def list_tasks(
        self,
        history_limit: int = 100,
        history_offset: int = 0,
        history_sort: str = "finished_at",
        history_status: str | None = None,
    ) -> dict[str, object]:
        if history_sort == "finished_at":
            history_order = "COALESCE(finished_at, created_at) DESC, id DESC"
        elif history_sort == "started_at":
            history_order = "COALESCE(started_at, created_at) DESC, id DESC"
        else:
            raise ValueError("历史排序字段无效")

        valid_history_statuses = {"succeeded", "failed", "cancelled", "interrupted"}
        if history_status is not None and history_status not in valid_history_statuses:
            raise ValueError("历史状态无效")

        safe_history_limit = max(1, int(history_limit))
        safe_history_offset = max(0, int(history_offset))
        history_where = "status NOT IN ('queued', 'running', 'staged')"
        history_params: list[object] = []
        if history_status is not None:
            history_where = "status = ?"
            history_params.append(history_status)

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
            staged = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'staged'
                ORDER BY queue_rank ASC, id ASC
                """
            ).fetchall()
            running = conn.execute(
                "SELECT * FROM tasks WHERE status = 'running' ORDER BY started_at ASC, id ASC"
            ).fetchall()
            history = conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE {history_where}
                ORDER BY {history_order}
                LIMIT ? OFFSET ?
                """,
                (*history_params, safe_history_limit, safe_history_offset),
            ).fetchall()
            status_count_rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM tasks
                GROUP BY status
                """
            ).fetchall()
            queued_count_rows = conn.execute(
                """
                SELECT queue_name, COUNT(*) AS count
                FROM tasks
                WHERE status = 'queued'
                GROUP BY queue_name
                """
            ).fetchall()
            history_filtered_count = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM tasks
                WHERE {history_where}
                """,
                tuple(history_params),
            ).fetchone()
            paused_value = conn.execute(
                "SELECT value FROM meta WHERE key = 'queue_paused'"
            ).fetchone()
        status_counts = {
            str(row["status"]): int(row["count"])
            for row in status_count_rows
        }
        queued_counts = {
            str(row["queue_name"] or NORMAL_QUEUE): int(row["count"])
            for row in queued_count_rows
        }
        normal_queued_count = queued_counts.get(NORMAL_QUEUE, 0)
        urgent_queued_count = queued_counts.get(URGENT_QUEUE, 0)
        staged_count = status_counts.get("staged", 0)
        running_count = status_counts.get("running", 0)
        history_count = sum(
            count
            for status, count in status_counts.items()
            if status not in {"queued", "running", "staged"}
        )
        total_count = sum(status_counts.values())
        filtered_count = (
            int(history_filtered_count["count"]) if history_filtered_count else 0
        )
        return {
            "queued": [self._row_to_task(row) for row in queued],
            "urgent_queued": [self._row_to_task(row) for row in urgent_queued],
            "staged": [self._row_to_task(row) for row in staged],
            "running": [self._row_to_task(row) for row in running],
            "history": [self._row_to_task(row) for row in history],
            "queue_paused": paused_value is not None and paused_value["value"] == "1",
            "history_limit": safe_history_limit,
            "history_offset": safe_history_offset,
            "counts": {
                "queued": normal_queued_count,
                "urgent_queued": urgent_queued_count,
                "staged": staged_count,
                "running": running_count,
                "history": history_count,
                "history_filtered": filtered_count,
                "total": total_count,
                "succeeded": status_counts.get("succeeded", 0),
                "failed": status_counts.get("failed", 0),
                "cancelled": status_counts.get("cancelled", 0),
                "interrupted": status_counts.get("interrupted", 0),
            },
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
        task_status = self._status_for_queue(normalized_queue_name)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = ? AND queue_name = ?
                ORDER BY queue_rank ASC, id ASC
                """,
                (task_status, normalized_queue_name),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_running_tasks(self) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'running' ORDER BY started_at ASC, id ASC"
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def list_task_attempts(self, task_id: int) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM task_attempts
                WHERE task_id = ?
                ORDER BY attempt ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_task_attempt(row) for row in rows]

    def list_task_operation_logs(self, task_id: int) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM operation_logs
                WHERE entity_type = 'task' AND entity_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_operation_log(row) for row in rows]

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
                "DELETE FROM task_dependencies WHERE task_id = ? OR depends_on_task_id = ?",
                (task_id, task_id),
            )
            conn.execute(
                "DELETE FROM task_attempts WHERE task_id = ?",
                (task_id,),
            )
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
        depends_on_ids: Sequence[int] | None = None,
    ) -> dict[str, object]:
        normalized_queue_name = self._normalize_queue_name(queue_name)
        task_status = self._status_for_queue(normalized_queue_name)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT queue_rank, queue_name, status
                FROM tasks
                WHERE id = ? AND status IN ('queued', 'staged')
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError("只能修改排队中或暂存中的任务")
            current_queue_name = self._normalize_queue_name(row["queue_name"])
            current_status = str(row["status"])
            if (
                current_queue_name == normalized_queue_name
                and current_status == task_status
                and row["queue_rank"] is not None
            ):
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
                    status = ?,
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
                WHERE id = ? AND status IN ('queued', 'staged')
                """,
                (
                    name,
                    command,
                    cwd,
                    json.dumps(env),
                    task_status,
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
            if depends_on_ids is not None:
                self._replace_dependencies(conn, task_id, depends_on_ids)
            conn.commit()
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def update_task_metadata(
        self,
        task_id: int,
        *,
        name: str,
        notes: str | None,
    ) -> dict[str, object]:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET name = ?,
                    notes = ?
                WHERE id = ?
                """,
                (name, notes, task_id),
            )
            conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(f"任务不存在: {task_id}")
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def move_task_to_queue(
        self,
        task_id: int,
        *,
        queue_name: str,
    ) -> dict[str, object]:
        normalized_queue_name = self._normalize_queue_name(queue_name)
        task_status = self._status_for_queue(normalized_queue_name)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT status, queue_name, queue_rank
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"任务不存在: {task_id}")
            current_status = str(row["status"])
            if current_status not in {"queued", "staged"}:
                raise ValueError("只有排队或暂存中的任务可以移动队列")
            current_queue_name = self._normalize_queue_name(row["queue_name"])
            if (
                current_status == task_status
                and current_queue_name == normalized_queue_name
                and row["queue_rank"] is not None
            ):
                queue_rank = int(row["queue_rank"])
            else:
                queue_rank = self._next_queue_rank(conn, normalized_queue_name)
            conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    queue_name = ?,
                    queue_rank = ?,
                    assigned_gpu = NULL,
                    pid = NULL,
                    exit_code = NULL,
                    finished_at = NULL,
                    next_retry_at = NULL
                WHERE id = ? AND status IN ('queued', 'staged')
                """,
                (task_status, normalized_queue_name, queue_rank, task_id),
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
        task_status = self._status_for_queue(normalized_queue_name)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM tasks
                WHERE status = ? AND queue_name = ?
                ORDER BY queue_rank ASC, id ASC
                """,
                (task_status, normalized_queue_name),
            ).fetchall()
            current_ids = [row["id"] for row in rows]
            if current_ids != list(task_ids):
                if set(current_ids) != set(task_ids) or len(current_ids) != len(task_ids):
                    raise ValueError("重排请求必须包含完整的队列任务列表")
            for idx, task_id in enumerate(task_ids, start=1):
                conn.execute(
                    """
                    UPDATE tasks
                    SET queue_rank = ?
                    WHERE id = ? AND status = ? AND queue_name = ?
                    """,
                    (idx, task_id, task_status, normalized_queue_name),
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
        auto_restore_idle_gpu_seconds: float | None,
        auto_retry_max_retries: int,
        auto_retry_delay_seconds: int,
        external_kill_gpu_cooldown_seconds: float,
    ) -> dict[str, object]:
        settings: dict[str, object] = {
            "poll_interval_seconds": poll_interval_seconds,
            "gpu_idle_required_checks": gpu_idle_required_checks,
            "auto_restore_idle_gpu_seconds": auto_restore_idle_gpu_seconds,
            "auto_retry_max_retries": auto_retry_max_retries,
            "auto_retry_delay_seconds": auto_retry_delay_seconds,
            "external_kill_gpu_cooldown_seconds": external_kill_gpu_cooldown_seconds,
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

    def create_agent_gpu_lease(
        self,
        *,
        lease_id: str,
        owner: str,
        gpu_ids: list[int],
        expires_at: str | None,
        notes: str | None,
    ) -> dict[str, object]:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_gpu_leases(
                    id, owner, gpu_ids, created_at, expires_at, released_at, notes
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                """,
                (lease_id, owner, json.dumps(gpu_ids), now, expires_at, notes),
            )
            conn.commit()
        lease = self.get_agent_gpu_lease(lease_id)
        if lease is None:
            raise ValueError(f"GPU lease 不存在: {lease_id}")
        return lease

    def get_agent_gpu_lease(self, lease_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_gpu_leases WHERE id = ?",
                (lease_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_agent_gpu_lease(row)

    def list_agent_gpu_leases(
        self,
        *,
        include_inactive: bool = False,
        now_iso: str | None = None,
    ) -> list[dict[str, object]]:
        now = now_iso or utc_now_iso()
        with self._lock, self._connect() as conn:
            if include_inactive:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_gpu_leases
                    ORDER BY created_at DESC, id DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM agent_gpu_leases
                    WHERE released_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                    ORDER BY created_at ASC, id ASC
                    """,
                    (now,),
                ).fetchall()
        return [self._row_to_agent_gpu_lease(row, now_iso=now) for row in rows]

    def release_agent_gpu_lease(self, lease_id: str) -> dict[str, object] | None:
        now = utc_now_iso()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_gpu_leases WHERE id = ?",
                (lease_id,),
            ).fetchone()
            if row is None:
                return None
            if row["released_at"] is None:
                conn.execute(
                    """
                    UPDATE agent_gpu_leases
                    SET released_at = ?
                    WHERE id = ? AND released_at IS NULL
                    """,
                    (now, lease_id),
                )
                conn.commit()
            row = conn.execute(
                "SELECT * FROM agent_gpu_leases WHERE id = ?",
                (lease_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_agent_gpu_lease(row, now_iso=now)


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
            row = conn.execute(
                "SELECT attempt_count FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is not None:
                self._upsert_task_attempt(
                    conn,
                    task_id=task_id,
                    attempt=int(row["attempt_count"] or 1),
                    started_at=started_at,
                    finished_at=None,
                    status="running",
                    exit_code=None,
                    log_path=log_path,
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
            row = conn.execute(
                "SELECT attempt_count, started_at, log_path FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
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
            if row is not None:
                self._upsert_task_attempt(
                    conn,
                    task_id=task_id,
                    attempt=max(1, int(row["attempt_count"] or 1)),
                    started_at=row["started_at"],
                    finished_at=finished_at,
                    status=status,
                    exit_code=exit_code,
                    log_path=row["log_path"],
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
        attempt = self._attempt_from_log_path(log_path)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT started_at FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            conn.execute(
                """
                UPDATE tasks
                SET status = 'failed',
                    queue_rank = NULL,
                    finished_at = ?,
                    exit_code = -1,
                    attempt_count = MAX(COALESCE(attempt_count, 0), ?),
                    log_path = ?,
                    notes = CASE
                        WHEN notes IS NULL OR notes = '' THEN ?
                        ELSE notes || CHAR(10) || ?
                    END
                WHERE id = ?
                """,
                (finished_at, attempt or 1, log_path, message, message, task_id),
            )
            if attempt is not None:
                self._upsert_task_attempt(
                    conn,
                    task_id=task_id,
                    attempt=attempt,
                    started_at=row["started_at"] if row is not None else None,
                    finished_at=finished_at,
                    status="failed",
                    exit_code=-1,
                    log_path=log_path,
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
        finished_at = utc_now_iso()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT attempt_count, started_at, log_path FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
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
                (queue_rank, exit_code, finished_at, next_retry_at, task_id),
            )
            if row is not None:
                self._upsert_task_attempt(
                    conn,
                    task_id=task_id,
                    attempt=max(1, int(row["attempt_count"] or 1)),
                    started_at=row["started_at"],
                    finished_at=finished_at,
                    status="retry_scheduled",
                    exit_code=exit_code,
                    log_path=row["log_path"],
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
        finished_at = utc_now_iso()
        with self._lock, self._connect() as conn:
            queue_rank = self._head_queue_rank(conn, normalized_queue_name)
            row = conn.execute(
                """
                SELECT attempt_count, started_at, log_path, exit_code
                FROM tasks
                WHERE id = ? AND status = 'running'
                """,
                (task_id,),
            ).fetchone()
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
            if row is not None:
                self._upsert_task_attempt(
                    conn,
                    task_id=task_id,
                    attempt=max(1, int(row["attempt_count"] or 1)),
                    started_at=row["started_at"],
                    finished_at=finished_at,
                    status="preempted",
                    exit_code=row["exit_code"],
                    log_path=row["log_path"],
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
        finished_at = utc_now_iso()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT attempt_count, started_at, log_path
                FROM tasks
                WHERE id = ? AND status = 'running'
                """,
                (task_id,),
            ).fetchone()
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
            if row is not None:
                self._upsert_task_attempt(
                    conn,
                    task_id=task_id,
                    attempt=max(1, int(row["attempt_count"] or 1)),
                    started_at=row["started_at"],
                    finished_at=finished_at,
                    status="interrupted_requeued",
                    exit_code=exit_code,
                    log_path=row["log_path"],
                )
            conn.commit()
        if cursor.rowcount == 0:
            raise ValueError("只有运行中的任务可以中断后回队列")
        task = self.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        return task

    def requeue_running_tasks_to_queue_head(self) -> int:
        finished_at = utc_now_iso()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, queue_name, attempt_count, started_at, log_path, exit_code
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
                    self._upsert_task_attempt(
                        conn,
                        task_id=int(row["id"]),
                        attempt=max(1, int(row["attempt_count"] or 1)),
                        started_at=row["started_at"],
                        finished_at=finished_at,
                        status="interrupted_requeued",
                        exit_code=row["exit_code"],
                        log_path=row["log_path"],
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
        normalized_queue_name = self._normalize_queue_name(queue_name)
        row = conn.execute(
            """
            SELECT COALESCE(MAX(queue_rank), 0) AS max_rank
            FROM tasks
            WHERE status = ? AND queue_name = ?
            """,
            (self._status_for_queue(normalized_queue_name), normalized_queue_name),
        ).fetchone()
        return int(row["max_rank"]) + 1

    def _head_queue_rank(self, conn: sqlite3.Connection, queue_name: str) -> int:
        normalized_queue_name = self._normalize_queue_name(queue_name)
        row = conn.execute(
            """
            SELECT MIN(queue_rank) AS min_rank
            FROM tasks
            WHERE status = ? AND queue_name = ?
            """,
            (self._status_for_queue(normalized_queue_name), normalized_queue_name),
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

    def _upsert_task_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: int,
        attempt: int,
        started_at: str | None,
        finished_at: str | None,
        status: str | None,
        exit_code: int | None,
        log_path: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_attempts(
                task_id, attempt, started_at, finished_at, status, exit_code, log_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, attempt) DO UPDATE SET
                started_at = COALESCE(excluded.started_at, task_attempts.started_at),
                finished_at = excluded.finished_at,
                status = COALESCE(excluded.status, task_attempts.status),
                exit_code = excluded.exit_code,
                log_path = COALESCE(excluded.log_path, task_attempts.log_path)
            """,
            (task_id, attempt, started_at, finished_at, status, exit_code, log_path),
        )

    def _attempt_from_log_path(self, log_path: str | None) -> int | None:
        if not log_path:
            return None
        name = Path(log_path).name
        marker = "_attempt_"
        if marker not in name:
            return None
        raw_attempt = name.rsplit(marker, 1)[-1].split(".", 1)[0]
        try:
            attempt = int(raw_attempt)
        except ValueError:
            return None
        return attempt if attempt > 0 else None

    def _normalize_queue_name(self, queue_name: str | None) -> str:
        value = str(queue_name or NORMAL_QUEUE).strip().lower()
        if value not in VALID_QUEUE_NAMES:
            raise ValueError(f"未知队列类型: {value}")
        return value

    def _status_for_queue(self, queue_name: str) -> str:
        return "staged" if self._normalize_queue_name(queue_name) == STAGED_QUEUE else "queued"

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

    def _row_to_task_attempt(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "task_id": row["task_id"],
            "attempt": row["attempt"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "exit_code": row["exit_code"],
            "log_path": row["log_path"],
        }

    def _row_to_operation_log(self, row: sqlite3.Row) -> dict[str, object]:
        metadata_raw = row["metadata"] or "{}"
        try:
            metadata = json.loads(metadata_raw)
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "level": row["level"],
            "source": row["source"],
            "action": row["action"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "title": row["title"],
            "detail": row["detail"],
            "metadata": metadata,
        }

    def _row_to_agent_gpu_lease(
        self,
        row: sqlite3.Row,
        *,
        now_iso: str | None = None,
    ) -> dict[str, object]:
        try:
            gpu_ids = json.loads(row["gpu_ids"] or "[]")
        except (TypeError, json.JSONDecodeError):
            gpu_ids = []
        if not isinstance(gpu_ids, list):
            gpu_ids = []
        normalized_gpu_ids: list[int] = []
        seen_gpu_ids: set[int] = set()
        for gpu_id in gpu_ids:
            try:
                value = int(gpu_id)
            except (TypeError, ValueError):
                continue
            if value in seen_gpu_ids:
                continue
            seen_gpu_ids.add(value)
            normalized_gpu_ids.append(value)
        normalized_gpu_ids.sort()
        status = "active"
        released_at = row["released_at"]
        expires_at = row["expires_at"]
        now = now_iso or utc_now_iso()
        if released_at is not None:
            status = "released"
        elif expires_at is not None and expires_at <= now:
            status = "expired"
        return {
            "id": row["id"],
            "owner": row["owner"],
            "gpu_ids": normalized_gpu_ids,
            "created_at": row["created_at"],
            "expires_at": expires_at,
            "released_at": released_at,
            "notes": row["notes"],
            "status": status,
        }

    # ── task dependencies ──────────────────────────────────────────

    def add_dependencies(
        self, task_id: int, depends_on_ids: Sequence[int]
    ) -> None:
        if not depends_on_ids:
            return
        with self._lock, self._connect() as conn:
            normalized_ids = self._normalize_dependency_ids(task_id, depends_on_ids)
            if not normalized_ids:
                return
            self._ensure_task_exists(conn, task_id)
            for dep_id in normalized_ids:
                if conn.execute(
                    "SELECT 1 FROM tasks WHERE id = ?", (dep_id,)
                ).fetchone() is None:
                    raise ValueError(f"依赖任务不存在: {dep_id}")
            if self._would_create_cycle(conn, task_id, normalized_ids):
                raise ValueError("添加依赖会形成循环")
            now = utc_now_iso()
            for dep_id in normalized_ids:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO task_dependencies(task_id, depends_on_task_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (task_id, dep_id, now),
                )
            conn.commit()

    def remove_dependencies(
        self,
        task_id: int,
        depends_on_ids: list[int] | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            if depends_on_ids is None:
                conn.execute(
                    "DELETE FROM task_dependencies WHERE task_id = ?",
                    (task_id,),
                )
            else:
                for dep_id in depends_on_ids:
                    conn.execute(
                        "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
                        (task_id, dep_id),
                    )
            conn.commit()

    def replace_dependencies(
        self, task_id: int, depends_on_ids: Sequence[int]
    ) -> None:
        with self._lock, self._connect() as conn:
            self._replace_dependencies(conn, task_id, depends_on_ids)
            conn.commit()

    def get_dependency_ids(self, task_id: int) -> list[int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT depends_on_task_id
                FROM task_dependencies
                WHERE task_id = ?
                ORDER BY depends_on_task_id ASC
                """,
                (task_id,),
            ).fetchall()
        return [int(row["depends_on_task_id"]) for row in rows]

    def get_dependencies(self, task_id: int) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM task_dependencies d
                JOIN tasks t ON t.id = d.depends_on_task_id
                WHERE d.task_id = ?
                ORDER BY d.depends_on_task_id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_dependents(self, task_id: int) -> list[dict[str, object]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM task_dependencies d
                JOIN tasks t ON t.id = d.task_id
                WHERE d.depends_on_task_id = ?
                ORDER BY d.task_id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def are_dependencies_satisfied(self, task_id: int) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN t.status = 'succeeded' THEN 1 ELSE 0 END) AS satisfied
                FROM task_dependencies d
                JOIN tasks t ON t.id = d.depends_on_task_id
                WHERE d.task_id = ?
                """,
                (task_id,),
            ).fetchone()
        total = int(row["total"] or 0)
        satisfied = int(row["satisfied"] or 0)
        return total == 0 or total == satisfied

    def get_dependency_count(self, task_id: int) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM task_dependencies WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["cnt"] or 0)

    def _would_create_cycle(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        depends_on_ids: Sequence[int],
    ) -> bool:
        for dep_id in depends_on_ids:
            row = conn.execute(
                """
                WITH RECURSIVE ancestors(id) AS (
                    SELECT depends_on_task_id
                    FROM task_dependencies WHERE task_id = ?
                    UNION
                    SELECT d.depends_on_task_id
                    FROM task_dependencies d
                    JOIN ancestors a ON d.task_id = a.id
                )
                SELECT 1 FROM ancestors WHERE id = ? LIMIT 1
                """,
                (dep_id, task_id),
            ).fetchone()
            if row is not None:
                return True
        return False

    def _replace_dependencies(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        depends_on_ids: Sequence[int],
    ) -> None:
        normalized_ids = self._normalize_dependency_ids(task_id, depends_on_ids)
        self._ensure_task_exists(conn, task_id)
        for dep_id in normalized_ids:
            self._ensure_task_exists(conn, dep_id, message=f"依赖任务不存在: {dep_id}")
        if self._would_create_cycle(conn, task_id, normalized_ids):
            raise ValueError("添加依赖会形成循环")
        conn.execute(
            "DELETE FROM task_dependencies WHERE task_id = ?",
            (task_id,),
        )
        if not normalized_ids:
            return
        now = utc_now_iso()
        for dep_id in normalized_ids:
            conn.execute(
                """
                INSERT INTO task_dependencies(task_id, depends_on_task_id, created_at)
                VALUES (?, ?, ?)
                """,
                (task_id, dep_id, now),
            )

    def _normalize_dependency_ids(
        self,
        task_id: int,
        depends_on_ids: Sequence[int],
    ) -> list[int]:
        normalized: list[int] = []
        seen: set[int] = set()
        for raw_id in depends_on_ids:
            dep_id = int(raw_id)
            if dep_id == task_id:
                raise ValueError("任务不能依赖自身")
            if dep_id in seen:
                continue
            seen.add(dep_id)
            normalized.append(dep_id)
        return normalized

    def _ensure_task_exists(
        self,
        conn: sqlite3.Connection,
        task_id: int,
        *,
        message: str | None = None,
    ) -> None:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise ValueError(message or f"任务不存在: {task_id}")
