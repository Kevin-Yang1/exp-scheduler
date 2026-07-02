from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import logging
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import subprocess
from typing import TYPE_CHECKING

from .database import Database, utc_now_iso
from .nodes import build_ssh_command, translate_ssh_error

if TYPE_CHECKING:
    from .nodes import ResolvedAuth


LOGGER = logging.getLogger("exp_scheduler")

SCHEDULER_TICK_INTERVAL = 60.0
RSYNC_TIMEOUT = 7200
BACKUP_LOG_DIR_NAME = "backup-logs"


class BackupService:
    """定时增量备份服务。

    使用 rsync --update（仅传修改过的文件）实现增量备份。
    支持每日/每周定时调度，也可手动触发。
    本地→本地直接 rsync；本地→远端或远端→本地走 SSH；
    远端→远端不在备份场景支持范围（备份目标应为本机或专用存储节点）。
    """

    def __init__(
        self,
        *,
        config,
        database: Database,
        events,
        node_resolver: Callable[[str], ResolvedAuth],
        known_hosts_path: Path | str,
        rsync_binary: str = "rsync",
        ssh_binary: str = "ssh",
        sshpass_binary: str = "sshpass",
    ) -> None:
        self.config = config
        self.database = database
        self.events = events
        self.node_resolver = node_resolver
        self.known_hosts_path = known_hosts_path
        self.rsync_binary = rsync_binary
        self.ssh_binary = ssh_binary
        self.sshpass_binary = sshpass_binary
        self._log_dir = config.log_dir / BACKUP_LOG_DIR_NAME
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[int, subprocess.Popen[bytes]] = {}
        self._watchers: dict[int, asyncio.Task[None]] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._lock = asyncio.Lock()

    async def startup(self) -> None:
        self._shutting_down = False
        self._compute_next_runs()
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="backup-scheduler"
        )

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None
        for run_id, proc in list(self._handles.items()):
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for task in self._watchers.values():
            task.cancel()
        await asyncio.gather(*self._watchers.values(), return_exceptions=True)
        self._handles.clear()
        self._watchers.clear()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def list_jobs(self) -> list[dict[str, object]]:
        jobs = self.database.list_backup_jobs()
        for job in jobs:
            run = self.database.list_backup_runs(job_id=str(job["id"]), limit=1)
            job["last_run"] = run[0] if run else None
        return jobs

    async def get_job(self, job_id: str) -> dict[str, object] | None:
        job = self.database.get_backup_job(job_id)
        if job is None:
            return None
        run = self.database.list_backup_runs(job_id=job_id, limit=1)
        job["last_run"] = run[0] if run else None
        return job

    async def create_job(
        self,
        *,
        name: str,
        src_node_id: str,
        src_path: str,
        dst_node_id: str,
        dst_path: str,
        schedule_type: str = "manual",
        schedule_hour: int = 2,
        schedule_minute: int = 0,
        schedule_day_of_week: int | None = None,
        enabled: bool = True,
        delete_extras: bool = False,
    ) -> dict[str, object]:
        if schedule_type not in ("manual", "daily", "weekly"):
            raise ValueError(f"无效的调度类型: {schedule_type}")
        if schedule_type == "weekly" and schedule_day_of_week is None:
            schedule_day_of_week = 0
        if not (0 <= schedule_hour <= 23):
            raise ValueError("schedule_hour 必须在 0-23 之间")
        if not (0 <= schedule_minute <= 59):
            raise ValueError("schedule_minute 必须在 0-59 之间")
        if schedule_type == "weekly" and not (0 <= schedule_day_of_week <= 6):
            raise ValueError("schedule_day_of_week 必须在 0-6 之间")
        name = name.strip()
        if not name:
            raise ValueError("备份任务名称不能为空")
        src_path = src_path.strip()
        dst_path = dst_path.strip()
        if not src_path or not dst_path:
            raise ValueError("源路径和目标路径不能为空")
        if src_path.startswith("-") or dst_path.startswith("-"):
            raise ValueError("路径不能以 - 开头")
        job_id = secrets.token_urlsafe(8)
        next_run = self._compute_next_run(
            schedule_type, schedule_hour, schedule_minute, schedule_day_of_week
        ) if enabled and schedule_type != "manual" else None
        job = self.database.create_backup_job(
            job_id=job_id,
            name=name,
            src_node_id=src_node_id,
            src_path=src_path,
            dst_node_id=dst_node_id,
            dst_path=dst_path,
            schedule_type=schedule_type,
            schedule_hour=schedule_hour,
            schedule_minute=schedule_minute,
            schedule_day_of_week=schedule_day_of_week,
            enabled=enabled,
            delete_extras=delete_extras,
            next_run_at=next_run,
        )
        await self.events.publish("backup_job_created", {"job_id": job_id, "job": job})
        return job

    async def update_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        enabled: bool | None = None,
        schedule_type: str | None = None,
        schedule_hour: int | None = None,
        schedule_minute: int | None = None,
        schedule_day_of_week: int | None = None,
        delete_extras: bool | None = None,
    ) -> dict[str, object] | None:
        if schedule_type is not None and schedule_type not in ("manual", "daily", "weekly"):
            raise ValueError(f"无效的调度类型: {schedule_type}")
        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("备份任务名称不能为空")
        job = self.database.get_backup_job(job_id)
        if job is None:
            raise ValueError(f"备份任务不存在: {job_id}")
        next_run = None
        effective_enabled = enabled if enabled is not None else bool(job["enabled"])
        effective_type = schedule_type or str(job["schedule_type"])
        effective_hour = schedule_hour if schedule_hour is not None else int(job["schedule_hour"])
        effective_minute = schedule_minute if schedule_minute is not None else int(job["schedule_minute"])
        effective_dow = schedule_day_of_week if schedule_day_of_week is not None else job["schedule_day_of_week"]
        if effective_enabled and effective_type != "manual":
            next_run = self._compute_next_run(
                effective_type, effective_hour, effective_minute, effective_dow
            )
        updated = self.database.update_backup_job(
            job_id,
            name=name,
            enabled=enabled,
            schedule_type=schedule_type,
            schedule_hour=schedule_hour,
            schedule_minute=schedule_minute,
            schedule_day_of_week=schedule_day_of_week,
            delete_extras=delete_extras,
            next_run_at=next_run,
        )
        if updated is not None:
            await self.events.publish("backup_job_updated", {"job_id": job_id, "job": updated})
        return updated

    async def delete_job(self, job_id: str) -> bool:
        job = self.database.get_backup_job(job_id)
        if job is None:
            raise ValueError(f"备份任务不存在: {job_id}")
        deleted = self.database.delete_backup_job(job_id)
        if deleted:
            await self.events.publish("backup_job_deleted", {"job_id": job_id})
        return deleted

    async def trigger_run(self, job_id: str) -> dict[str, object]:
        job = self.database.get_backup_job(job_id)
        if job is None:
            raise ValueError(f"备份任务不存在: {job_id}")
        run = await self._execute_backup(job, manual=True)
        return run

    async def list_runs(self, job_id: str | None = None, limit: int = 100) -> list[dict[str, object]]:
        return self.database.list_backup_runs(job_id=job_id, limit=limit)

    async def read_run_log(self, run_id: int, *, tail_bytes: int | None = 64 * 1024) -> dict[str, object]:
        run = self.database.get_backup_run(run_id)
        if run is None:
            raise ValueError(f"备份运行记录不存在: {run_id}")
        log_path = run.get("log_path")
        if not log_path or not Path(log_path).exists():
            return {"content": "", "log_path": log_path, "size": 0}
        path = Path(log_path)
        size = path.stat().st_size
        if tail_bytes is not None and size > tail_bytes:
            with path.open("rb") as fh:
                fh.seek(size - tail_bytes)
                content = fh.read()
        else:
            content = path.read_bytes()
        return {"content": content.decode("utf-8", errors="replace"), "log_path": log_path, "size": size}

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    def _compute_next_run(
        self,
        schedule_type: str,
        hour: int,
        minute: int,
        day_of_week: int | None,
    ) -> str:
        now = datetime.now(UTC)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if schedule_type == "daily":
            if target <= now:
                target += timedelta(days=1)
        elif schedule_type == "weekly":
            if day_of_week is None:
                day_of_week = 0
            current_dow = now.weekday()
            days_ahead = day_of_week - current_dow
            if days_ahead < 0:
                days_ahead += 7
            target = target + timedelta(days=days_ahead)
            if target <= now:
                target += timedelta(days=7)
        else:
            return utc_now_iso()
        return target.isoformat()

    def _compute_next_runs(self) -> None:
        jobs = self.database.list_enabled_backup_jobs()
        for job in jobs:
            if str(job["schedule_type"]) == "manual":
                continue
            next_run = self._compute_next_run(
                str(job["schedule_type"]),
                int(job["schedule_hour"]),
                int(job["schedule_minute"]),
                job["schedule_day_of_week"],
            )
            self.database.update_backup_job(str(job["id"]), next_run_at=next_run)

    async def _scheduler_loop(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(SCHEDULER_TICK_INTERVAL)
                await self._check_schedules()
            except asyncio.CancelledError:
                break
            except Exception:
                LOGGER.debug("Backup scheduler error", exc_info=True)

    async def _check_schedules(self) -> None:
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        jobs = self.database.list_enabled_backup_jobs()
        for job in jobs:
            if str(job["schedule_type"]) == "manual":
                continue
            next_run_str = job.get("next_run_at")
            if not next_run_str:
                continue
            try:
                next_run = datetime.fromisoformat(next_run_str)
            except ValueError:
                continue
            if next_run <= now:
                async with self._lock:
                    running = any(
                        str(j["id"]) == str(job["id"])
                        for j in self.database.list_backup_jobs()
                        if self._is_job_running(str(job["id"]))
                    )
                    if running:
                        continue
                await self._execute_backup(job, manual=False)

    def _is_job_running(self, job_id: str) -> bool:
        for proc in self._handles.values():
            if proc.poll() is None:
                return True
        return False

    # ------------------------------------------------------------------
    # rsync 执行
    # ------------------------------------------------------------------

    async def _execute_backup(self, job: dict[str, object], *, manual: bool) -> dict[str, object]:
        job_id = str(job["id"])
        started_at = utc_now_iso()
        log_path = self._log_dir / f"backup_{job_id}_{int(datetime.now(UTC).timestamp())}.log"
        run = self.database.create_backup_run(
            job_id=job_id, started_at=started_at, log_path=str(log_path)
        )
        run_id = int(run["id"])
        await self.events.publish(
            "backup_run_started",
            {"job_id": job_id, "run_id": run_id, "manual": manual},
        )
        if not manual:
            next_run = self._compute_next_run(
                str(job["schedule_type"]),
                int(job["schedule_hour"]),
                int(job["schedule_minute"]),
                job["schedule_day_of_week"],
            )
            self.database.update_backup_job(job_id, last_run_at=started_at, next_run_at=next_run)
        else:
            self.database.update_backup_job(job_id, last_run_at=started_at)

        task = asyncio.create_task(
            self._run_rsync(job, run_id, log_path), name=f"backup-run-{run_id}"
        )
        self._watchers[run_id] = task
        return run

    async def _run_rsync(
        self,
        job: dict[str, object],
        run_id: int,
        log_path: Path,
    ) -> None:
        job_id = str(job["id"])
        src_node_id = str(job["src_node_id"])
        src_path = str(job["src_path"])
        dst_node_id = str(job["dst_node_id"])
        dst_path = str(job["dst_path"])
        delete_extras = bool(job["delete_extras"])
        src_auth = self.node_resolver(src_node_id)
        dst_auth = self.node_resolver(dst_node_id)

        try:
            argv, env_extra = self._build_rsync_command(
                src_auth, src_path, dst_auth, dst_path, delete_extras
            )
        except ValueError as exc:
            await self._finalize_run(run_id, job_id, "failed", exit_code=-1, error=str(exc), log_path=log_path)
            return

        env = os.environ.copy()
        env.update(env_extra)
        redacted = self._redact_command(argv)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w+b")
        log_file.write(f"[exp-scheduler] backup job={job_id} run={run_id}\n".encode())
        log_file.write(f"[exp-scheduler] command: {redacted}\n".encode())
        log_file.flush()

        try:
            proc = subprocess.Popen(
                argv,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except Exception as exc:
            log_file.write(f"[exp-scheduler] 启动失败: {exc}\n".encode())
            log_file.close()
            await self._finalize_run(run_id, job_id, "failed", exit_code=-1, error=str(exc), log_path=log_path)
            return

        self._handles[run_id] = proc
        try:
            exit_code = await asyncio.to_thread(proc.wait, timeout=RSYNC_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                exit_code = await asyncio.to_thread(proc.wait, timeout=10)
            except Exception:
                exit_code = -1
            log_file.write("[exp-scheduler] rsync 超时，已终止\n".encode())
        except Exception as exc:
            log_file.write(f"[exp-scheduler] 等待异常: {exc}\n".encode())
            exit_code = -1
        finally:
            self._handles.pop(run_id, None)
            log_file.flush()
            log_file.close()

        status = "succeeded" if exit_code == 0 else "failed"
        error = None if exit_code == 0 else f"rsync exit_code={exit_code}"
        await self._finalize_run(
            run_id, job_id, status, exit_code=exit_code, error=error, log_path=log_path
        )

    async def _finalize_run(
        self,
        run_id: int,
        job_id: str,
        status: str,
        *,
        exit_code: int | None = None,
        error: str | None = None,
        log_path: Path | None = None,
    ) -> None:
        finished_at = utc_now_iso()
        self.database.update_backup_run(
            run_id,
            status=status,
            finished_at=finished_at,
            exit_code=exit_code,
            error=error,
        )
        await self.events.publish(
            "backup_run_finished",
            {
                "job_id": job_id,
                "run_id": run_id,
                "status": status,
                "exit_code": exit_code,
                "error": error,
            },
        )

    def _build_rsync_command(
        self,
        src_auth: ResolvedAuth,
        src_path: str,
        dst_auth: ResolvedAuth,
        dst_path: str,
        delete_extras: bool,
    ) -> tuple[list[str], dict[str, str]]:
        argv: list[str] = [
            self.rsync_binary,
            "-a",
            "--update",
            "--partial-dir=.rsync-partial",
            "--info=progress2",
            "--info=name1",
            "-s",
            "--timeout=300",
        ]
        if delete_extras:
            argv.append("--delete")
        env_extra: dict[str, str] = {}

        if src_auth.is_local and dst_auth.is_local:
            argv.append(self._ensure_trailing_slash(src_path))
            argv.append(dst_path)
            return argv, env_extra

        if src_auth.is_local and not dst_auth.is_local:
            ssh_argv, ssh_env = build_ssh_command(
                dst_auth,
                known_hosts_path=self.known_hosts_path,
                remote_command=None,
                ssh_binary=self.ssh_binary,
                sshpass_binary=self.sshpass_binary,
            )
            env_extra.update(ssh_env)
            ssh_str = " ".join(shlex.quote(a) for a in ssh_argv)
            argv += ["-e", ssh_str]
            argv.append(self._ensure_trailing_slash(src_path))
            argv.append(f"{dst_auth.username}@{dst_auth.host}:{dst_path}")
            return argv, env_extra

        if not src_auth.is_local and dst_auth.is_local:
            ssh_argv, ssh_env = build_ssh_command(
                src_auth,
                known_hosts_path=self.known_hosts_path,
                remote_command=None,
                ssh_binary=self.ssh_binary,
                sshpass_binary=self.sshpass_binary,
            )
            env_extra.update(ssh_env)
            ssh_str = " ".join(shlex.quote(a) for a in ssh_argv)
            argv += ["-e", ssh_str]
            argv.append(f"{src_auth.username}@{src_auth.host}:{src_path}")
            argv.append(dst_path)
            return argv, env_extra

        raise ValueError(
            "远端→远端备份不支持，请使用本机或专用存储节点作为备份目标"
        )

    @staticmethod
    def _ensure_trailing_slash(path: str) -> str:
        if path == "/" or path.endswith("/"):
            return path
        return path + "/"

    @staticmethod
    def _redact_command(argv: list[str]) -> str:
        parts: list[str] = []
        skip_next = False
        for i, arg in enumerate(argv):
            if skip_next:
                parts.append("***")
                skip_next = False
                continue
            if arg == "sshpass":
                parts.append(arg)
                if i + 1 < len(argv) and argv[i + 1] == "-e":
                    parts.append("-e")
                    skip_next = False
                    continue
            if "SSHPASS" in arg:
                parts.append("***")
                continue
            parts.append(shlex.quote(arg))
        return " ".join(parts)
