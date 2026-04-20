from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import os
from pathlib import Path
import re
import signal
import traceback
from typing import Callable

from .config import SchedulerConfig
from .database import Database
from .events import EventBroker
from .gpu import GPUInfo, query_gpus
from .profile_discovery import discover_installed_environments


LOGGER = logging.getLogger("exp_scheduler")
LOG_TAIL_BYTES = 32 * 1024
TERMINATE_GRACE_SECONDS = 5
RETRYABLE_OOM_PATTERN = re.compile(
    r"out of memory|cuda out of memory|cublas.*alloc|cuda error: out of memory|"
    r"failed to allocate|cuda runtime error|memory allocation|std::bad_alloc|"
    r"nccl.*unhandled system error|device-side assert triggered|resource exhausted|"
    r"cuda error.*launch out of resources|killed|terminated|oom-kill|"
    r"out of memory: kill process",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ProcessHandle:
    task_id: int
    gpu_id: int
    process: asyncio.subprocess.Process
    log_file: object
    log_path: Path
    attempt_count: int
    stop_reason: str | None = None


class SchedulerService:
    def __init__(
        self,
        *,
        config: SchedulerConfig,
        database: Database,
        gpu_provider: Callable[[], list[GPUInfo]] | None = None,
        profile_discovery_provider: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.events = EventBroker()
        self._gpu_provider = gpu_provider or query_gpus
        self._profile_discovery_provider = (
            profile_discovery_provider or discover_installed_environments
        )
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._running: dict[int, ProcessHandle] = {}
        self._watchers: dict[int, asyncio.Task[None]] = {}
        self._last_gpu_payload: list[dict[str, object]] = []

    async def startup(self) -> None:
        self.database.init()
        interrupted = self.database.mark_running_tasks_interrupted()
        if interrupted:
            LOGGER.info("Marked %s stale running tasks as interrupted", interrupted)
        self._stop_event.clear()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="scheduler-loop")
        await self.events.publish("service_started", {"interrupted": interrupted})

    async def shutdown(self) -> None:
        self._stop_event.set()
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        await self._interrupt_running_tasks()
        if self._watchers:
            await asyncio.gather(*self._watchers.values(), return_exceptions=True)
        await self.events.publish("service_stopped", {})

    async def list_tasks(self) -> dict[str, object]:
        return self.database.list_tasks()

    async def list_gpus(self) -> list[dict[str, object]]:
        if self._last_gpu_payload:
            return self._last_gpu_payload
        return await self._refresh_gpu_payload()

    async def create_task(
        self,
        *,
        name: str | None,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        notes: str | None,
        requested_gpu: int | None = None,
        profile_id: int | None = None,
    ) -> dict[str, object]:
        normalized_requested_gpu = await self._normalize_requested_gpu(requested_gpu)
        final_name = (name or "").strip() or command.strip()[:80]
        profile_name: str | None = None
        shell_setup: str | None = None
        resolved_cwd = cwd
        resolved_env = dict(env)
        if profile_id is not None:
            profile = self.database.get_profile(profile_id)
            if profile is None:
                raise ValueError("环境配置不存在")
            profile_name = str(profile["name"])
            shell_setup = (
                str(profile["shell_setup"]).strip()
                if isinstance(profile["shell_setup"], str) and profile["shell_setup"].strip()
                else None
            )
            if not resolved_cwd and isinstance(profile["cwd"], str) and profile["cwd"].strip():
                resolved_cwd = profile["cwd"]
            resolved_env = dict(profile["env"])
            resolved_env.update({key: str(value) for key, value in env.items()})
        task = self.database.create_task(
            name=final_name,
            command=command,
            cwd=resolved_cwd,
            env=resolved_env,
            notes=notes,
            requested_gpu=normalized_requested_gpu,
            profile_id=profile_id,
            profile_name=profile_name,
            shell_setup=shell_setup,
        )
        await self.events.publish("task_created", {"task_id": task["id"]})
        return task

    async def list_profiles(self) -> list[dict[str, object]]:
        return self.database.list_profiles()

    async def discover_profiles(self) -> dict[str, object]:
        return await asyncio.to_thread(self._profile_discovery_provider)

    async def create_profile(
        self,
        *,
        name: str,
        cwd: str | None,
        env: dict[str, str],
        shell_setup: str | None,
        notes: str | None,
    ) -> dict[str, object]:
        profile = self.database.create_profile(
            name=name.strip(),
            cwd=cwd.strip() if isinstance(cwd, str) and cwd.strip() else None,
            env={key: str(value) for key, value in env.items()},
            shell_setup=shell_setup.strip()
            if isinstance(shell_setup, str) and shell_setup.strip()
            else None,
            notes=notes.strip() if isinstance(notes, str) and notes.strip() else None,
        )
        await self.events.publish("profile_created", {"profile_id": profile["id"]})
        return profile

    async def import_profile(
        self,
        *,
        name: str,
        cwd: str | None,
        env: dict[str, str],
        shell_setup: str | None,
        notes: str | None,
    ) -> tuple[dict[str, object], str | None]:
        base_name = name.strip()
        if not base_name:
            raise ValueError("环境配置名称不能为空")
        candidate_name = base_name
        renamed_from: str | None = None
        suffix = 2
        while True:
            try:
                profile = await self.create_profile(
                    name=candidate_name,
                    cwd=cwd,
                    env=env,
                    shell_setup=shell_setup,
                    notes=notes,
                )
                return profile, renamed_from
            except ValueError as exc:
                if "名称已存在" not in str(exc):
                    raise
                renamed_from = base_name
                candidate_name = f"{base_name}-{suffix}"
                suffix += 1

    async def update_profile(
        self,
        profile_id: int,
        *,
        name: str,
        cwd: str | None,
        env: dict[str, str],
        shell_setup: str | None,
        notes: str | None,
    ) -> dict[str, object]:
        profile = self.database.update_profile(
            profile_id,
            name=name.strip(),
            cwd=cwd.strip() if isinstance(cwd, str) and cwd.strip() else None,
            env={key: str(value) for key, value in env.items()},
            shell_setup=shell_setup.strip()
            if isinstance(shell_setup, str) and shell_setup.strip()
            else None,
            notes=notes.strip() if isinstance(notes, str) and notes.strip() else None,
        )
        await self.events.publish("profile_updated", {"profile_id": profile["id"]})
        return profile

    async def delete_profile(self, profile_id: int) -> None:
        deleted = self.database.delete_profile(profile_id)
        if not deleted:
            raise ValueError("环境配置不存在")
        await self.events.publish("profile_deleted", {"profile_id": profile_id})

    async def delete_task(self, task_id: int) -> None:
        deleted = self.database.delete_queued_task(task_id)
        if not deleted:
            raise ValueError("只能删除排队中的任务")
        await self.events.publish("task_deleted", {"task_id": task_id})

    async def reorder_tasks(self, task_ids: list[int]) -> list[dict[str, object]]:
        queue = self.database.reorder_queue(task_ids)
        await self.events.publish("queue_reordered", {"task_ids": task_ids})
        return queue

    async def cancel_task(self, task_id: int) -> None:
        async with self._lock:
            handle = self._running.get(task_id)
            if handle is None:
                raise ValueError("只有运行中的任务可以取消")
            handle.stop_reason = "cancel"
            self._signal_process_group(handle.process.pid, signal.SIGTERM)
            asyncio.create_task(self._escalate_kill(handle.task_id), name=f"cancel-{task_id}")
        await self.events.publish("task_cancelling", {"task_id": task_id})

    async def requeue_task(self, task_id: int) -> dict[str, object]:
        task = self.database.clone_task_for_requeue(task_id)
        await self.events.publish("task_requeued", {"task_id": task["id"], "source_task_id": task_id})
        return task

    async def set_queue_paused(self, paused: bool) -> bool:
        result = self.database.set_queue_paused(paused)
        await self.events.publish(
            "queue_paused" if paused else "queue_resumed",
            {"paused": result},
        )
        return result

    async def get_settings(self) -> dict[str, object]:
        return {"allowed_gpu_ids": self.database.get_allowed_gpu_ids()}

    async def update_settings(self, *, allowed_gpu_ids: list[int] | None) -> dict[str, object]:
        normalized_allowed_gpu_ids = await self._normalize_allowed_gpu_ids(allowed_gpu_ids)
        self.database.set_allowed_gpu_ids(normalized_allowed_gpu_ids)
        await self.events.publish(
            "settings_updated",
            {"allowed_gpu_ids": normalized_allowed_gpu_ids},
        )
        await self._trigger_immediate_schedule()
        return await self.get_settings()

    async def read_task_log(self, task_id: int, *, tail_bytes: int = LOG_TAIL_BYTES) -> dict[str, object]:
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        log_path = task.get("log_path")
        content = ""
        if isinstance(log_path, str) and log_path:
            content = read_text_tail(Path(log_path), tail_bytes=tail_bytes)
        return {"task": task, "content": content}

    async def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Scheduler tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        payload = await self._refresh_gpu_payload()
        async with self._lock:
            if self.database.get_queue_paused():
                return
            queued_tasks = [
                task
                for task in self.database.list_queued_tasks()
                if self._is_task_ready_for_launch(task)
            ]
            if not queued_tasks:
                return
            available = [
                gpu for gpu in payload if gpu["is_idle"] and not gpu["scheduler_occupied"]
            ]
            if not available:
                return
            for task, gpu in self._match_tasks_to_gpus(queued_tasks, available):
                await self._launch_task(task, gpu)

    async def _refresh_gpu_payload(self) -> list[dict[str, object]]:
        gpus = await asyncio.to_thread(self._gpu_provider)
        occupied = {handle.gpu_id for handle in self._running.values()}
        allowed_gpu_ids = self.database.get_allowed_gpu_ids()
        allowed_gpu_set = set(allowed_gpu_ids) if allowed_gpu_ids is not None else None
        payload = [
            gpu.to_dict(
                threshold_mb=self.config.gpu_idle_memory_mb,
                scheduler_occupied=gpu.index in occupied,
                globally_enabled=allowed_gpu_set is None or gpu.index in allowed_gpu_set,
            )
            for gpu in gpus
        ]
        if payload != self._last_gpu_payload:
            self._last_gpu_payload = payload
            await self.events.publish("gpu_updated", {"gpus": payload})
        return payload

    async def _launch_task(
        self,
        task: dict[str, object],
        gpu: dict[str, object],
    ) -> None:
        task_id = int(task["id"])
        gpu_id = int(gpu["index"])
        next_attempt = int(task.get("attempt_count") or 0) + 1
        log_path = self._log_path_for_task(task_id, next_attempt)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8")
        log_file.write(
            f"[exp-scheduler] task={task_id} gpu={gpu_id} started\n"
            f"[exp-scheduler] command={task['command']}\n"
            f"[exp-scheduler] attempt={next_attempt}/{self.config.auto_retry_max_retries + 1}\n"
        )
        if isinstance(task.get("profile_name"), str) and task["profile_name"]:
            log_file.write(f"[exp-scheduler] profile={task['profile_name']}\n")
        log_file.flush()

        env = os.environ.copy()
        env.update({key: str(value) for key, value in dict(task["env"]).items()})
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        env["EXP_SCHEDULER_ATTEMPT"] = str(next_attempt)
        env["EXP_SCHEDULER_MAX_RETRIES"] = str(self.config.auto_retry_max_retries)
        cwd = task["cwd"] if isinstance(task["cwd"], str) and task["cwd"] else None
        launch_command = self._build_launch_command(task)

        try:
            process = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                launch_command,
                cwd=cwd,
                env=env,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
        except Exception as exc:
            log_file.write("[exp-scheduler] launch failed\n")
            log_file.write("".join(traceback.format_exception(exc)))
            log_file.flush()
            log_file.close()
            self.database.mark_task_launch_failed(
                task_id=task_id,
                log_path=str(log_path),
                message=f"启动失败: {exc}",
            )
            await self.events.publish("task_failed_to_launch", {"task_id": task_id})
            return

        running_task = self.database.mark_task_running(
            task_id=task_id,
            gpu_id=gpu_id,
            pid=process.pid,
            log_path=str(log_path),
        )
        handle = ProcessHandle(
            task_id=task_id,
            gpu_id=gpu_id,
            process=process,
            log_file=log_file,
            log_path=log_path,
            attempt_count=int(running_task.get("attempt_count") or next_attempt),
        )
        self._running[task_id] = handle
        self._watchers[task_id] = asyncio.create_task(
            self._watch_process(handle),
            name=f"watch-task-{task_id}",
        )
        await self.events.publish(
            "task_started",
            {
                "task_id": task_id,
                "gpu_id": gpu_id,
                "pid": running_task["pid"],
            },
        )

    async def _watch_process(self, handle: ProcessHandle) -> None:
        task_id = handle.task_id
        try:
            exit_code = await handle.process.wait()
            handle.log_file.flush()
            if handle.stop_reason == "cancel":
                status = "cancelled"
            elif handle.stop_reason == "interrupt":
                status = "interrupted"
            else:
                status = "succeeded" if exit_code == 0 else "failed"

            if (
                status == "failed"
                and self._should_retry_task(handle=handle, exit_code=exit_code)
            ):
                next_retry_at = self._next_retry_at()
                handle.log_file.write(
                    "\n[exp-scheduler] retry_scheduled=true "
                    f"next_retry_at={next_retry_at} "
                    f"attempt={handle.attempt_count}/{self.config.auto_retry_max_retries + 1}\n"
                )
                handle.log_file.flush()
                self.database.schedule_task_retry(
                    task_id=task_id,
                    next_retry_at=next_retry_at,
                    exit_code=exit_code,
                )
                await self.events.publish(
                    "task_retry_scheduled",
                    {
                        "task_id": task_id,
                        "attempt_count": handle.attempt_count,
                        "max_retries": self.config.auto_retry_max_retries,
                        "next_retry_at": next_retry_at,
                        "exit_code": exit_code,
                    },
                )
                return

            handle.log_file.write(
                f"\n[exp-scheduler] task={task_id} finished status={status} exit_code={exit_code}\n"
            )
            handle.log_file.flush()
            self.database.finish_task(
                task_id=task_id,
                status=status,
                exit_code=exit_code,
                pid=None,
            )
            await self.events.publish(
                "task_finished",
                {
                    "task_id": task_id,
                    "status": status,
                    "exit_code": exit_code,
                },
            )
        finally:
            handle.log_file.close()
            async with self._lock:
                self._running.pop(task_id, None)
                self._watchers.pop(task_id, None)
            await self._trigger_immediate_schedule()

    def _match_tasks_to_gpus(
        self,
        queued_tasks: list[dict[str, object]],
        available_gpus: list[dict[str, object]],
    ) -> list[tuple[dict[str, object], dict[str, object]]]:
        remaining_gpus = list(available_gpus)
        assignments: list[tuple[dict[str, object], dict[str, object]]] = []
        for task in queued_tasks:
            requested_gpu = task.get("requested_gpu")
            chosen_index: int | None = None
            if isinstance(requested_gpu, int):
                for index, gpu in enumerate(remaining_gpus):
                    if int(gpu["index"]) == requested_gpu:
                        chosen_index = index
                        break
            elif remaining_gpus:
                chosen_index = 0
            if chosen_index is None:
                continue
            assignments.append((task, remaining_gpus.pop(chosen_index)))
            if not remaining_gpus:
                break
        return assignments

    async def _interrupt_running_tasks(self) -> None:
        async with self._lock:
            handles = list(self._running.values())
            for handle in handles:
                handle.stop_reason = "interrupt"
                self._signal_process_group(handle.process.pid, signal.SIGTERM)
        if handles:
            await asyncio.sleep(min(self.config.poll_interval_seconds, TERMINATE_GRACE_SECONDS))
            for handle in handles:
                if handle.process.returncode is None:
                    self._signal_process_group(handle.process.pid, signal.SIGKILL)

    async def _escalate_kill(self, task_id: int) -> None:
        await asyncio.sleep(TERMINATE_GRACE_SECONDS)
        async with self._lock:
            handle = self._running.get(task_id)
            if handle is None or handle.process.returncode is not None:
                return
            self._signal_process_group(handle.process.pid, signal.SIGKILL)

    def _signal_process_group(self, pid: int | None, sig: signal.Signals) -> None:
        if pid is None:
            return
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return

    def _log_path_for_task(self, task_id: int, attempt_count: int) -> Path:
        return self.config.log_dir / f"task_{task_id}_attempt_{attempt_count}.log"

    def _is_task_ready_for_launch(self, task: dict[str, object]) -> bool:
        next_retry_at = task.get("next_retry_at")
        if not isinstance(next_retry_at, str) or not next_retry_at:
            return True
        try:
            retry_time = datetime.fromisoformat(next_retry_at)
        except ValueError:
            return True
        return retry_time <= datetime.now(UTC)

    def _should_retry_task(
        self,
        *,
        handle: ProcessHandle,
        exit_code: int,
    ) -> bool:
        if self.config.auto_retry_max_retries <= 0:
            return False
        retries_used = max(0, handle.attempt_count - 1)
        if retries_used >= self.config.auto_retry_max_retries:
            return False
        return is_retryable_oom_error(exit_code, handle.log_path)

    def _next_retry_at(self) -> str:
        return (
            datetime.now(UTC)
            + timedelta(seconds=max(0, self.config.auto_retry_delay_seconds))
        ).isoformat()

    async def _normalize_requested_gpu(self, requested_gpu: int | None) -> int | None:
        if requested_gpu is None:
            return None
        if requested_gpu < 0:
            raise ValueError("指定 GPU 必须是非负整数")
        known_gpu_ids = await self._known_gpu_ids()
        if requested_gpu not in known_gpu_ids:
            raise ValueError(f"GPU 不存在: {requested_gpu}")
        return requested_gpu

    async def _normalize_allowed_gpu_ids(
        self,
        allowed_gpu_ids: list[int] | None,
    ) -> list[int] | None:
        if allowed_gpu_ids is None:
            return None
        normalized: list[int] = []
        seen: set[int] = set()
        for gpu_id in allowed_gpu_ids:
            value = int(gpu_id)
            if value < 0:
                raise ValueError("GPU 列表里只能包含非负整数")
            if value in seen:
                continue
            normalized.append(value)
            seen.add(value)
        known_gpu_ids = await self._known_gpu_ids()
        missing = [gpu_id for gpu_id in normalized if gpu_id not in known_gpu_ids]
        if missing:
            missing_text = ", ".join(str(item) for item in missing)
            raise ValueError(f"GPU 不存在: {missing_text}")
        return normalized

    async def _known_gpu_ids(self) -> set[int]:
        gpus = await asyncio.to_thread(self._gpu_provider)
        return {gpu.index for gpu in gpus}

    async def _trigger_immediate_schedule(self) -> None:
        if self._stop_event.is_set():
            return
        try:
            await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Immediate reschedule failed")

    def _build_launch_command(self, task: dict[str, object]) -> str:
        shell_setup = (
            str(task["shell_setup"]).strip()
            if isinstance(task.get("shell_setup"), str) and str(task["shell_setup"]).strip()
            else ""
        )
        command = str(task["command"])
        if not shell_setup:
            return command
        return "\n".join(
            [
                "set -e",
                shell_setup,
                command,
            ]
        )


def read_text_tail(path: Path, *, tail_bytes: int = LOG_TAIL_BYTES) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - tail_bytes))
        data = fh.read()
    return data.decode("utf-8", errors="replace")


def is_retryable_oom_error(exit_code: int, log_path: Path) -> bool:
    if exit_code in {137, 143, -9, -15}:
        return True
    if not log_path.exists():
        return False
    content = log_path.read_text(encoding="utf-8", errors="replace")
    return bool(RETRYABLE_OOM_PATTERN.search(content))
