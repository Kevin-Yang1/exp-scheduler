from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import errno
import logging
import os
from pathlib import Path
import pty
import re
import signal
import subprocess
import sys
import traceback
from typing import Callable

from .config import SchedulerConfig
from .database import Database, NORMAL_QUEUE, URGENT_QUEUE
from .events import EventBroker
from .gpu import GPUInfo, query_gpus
from .profile_discovery import discover_installed_environments
from .terminal import (
    DEFAULT_TERMINAL_COLUMNS,
    DEFAULT_TERMINAL_ROWS,
    TERMINAL_CHUNK_BYTES,
    TERMINAL_SNAPSHOT_BYTES,
    TerminalSession,
    TerminalSubscriber,
    read_text,
    read_text_tail,
    set_terminal_window_size,
)


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
    process: subprocess.Popen[bytes]
    log_path: Path
    attempt_count: int
    terminal_session: TerminalSession
    stop_reason: str | None = None
    requeue_to_queue_name: str | None = None


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
        self._terminal_sessions: dict[int, TerminalSession] = {}
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
        if self._terminal_sessions:
            await asyncio.gather(
                *[
                    self._close_terminal_session(session, exit_payload=None)
                    for session in list(self._terminal_sessions.values())
                ],
                return_exceptions=True,
            )
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
        is_urgent: bool = False,
        requested_gpu: int | None = None,
        profile_id: int | None = None,
    ) -> dict[str, object]:
        normalized_requested_gpu = await self._normalize_requested_gpu(requested_gpu)
        queue_name = URGENT_QUEUE if is_urgent else NORMAL_QUEUE
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
            queue_name=queue_name,
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
        async with self._lock:
            task = self.database.delete_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        log_path = task.get("log_path")
        if isinstance(log_path, str) and log_path:
            try:
                Path(log_path).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("Failed to delete log file for task %s: %s", task_id, log_path)
        await self.events.publish(
            "task_deleted",
            {"task_id": task_id, "status": task["status"]},
        )

    async def update_task(
        self,
        task_id: int,
        *,
        name: str | None,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        notes: str | None,
        is_urgent: bool = False,
        requested_gpu: int | None = None,
        profile_id: int | None = None,
    ) -> dict[str, object]:
        normalized_requested_gpu = await self._normalize_requested_gpu(requested_gpu)
        queue_name = URGENT_QUEUE if is_urgent else NORMAL_QUEUE
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
        task = self.database.update_queued_task(
            task_id,
            name=final_name,
            command=command,
            cwd=resolved_cwd,
            env=resolved_env,
            notes=notes,
            requested_gpu=normalized_requested_gpu,
            queue_name=queue_name,
            profile_id=profile_id,
            profile_name=profile_name,
            shell_setup=shell_setup,
        )
        await self.events.publish("task_updated", {"task_id": task["id"]})
        await self._trigger_immediate_schedule()
        return task

    async def reorder_tasks(
        self,
        task_ids: list[int],
        *,
        queue_name: str = NORMAL_QUEUE,
    ) -> list[dict[str, object]]:
        queue = self.database.reorder_queue(task_ids, queue_name=queue_name)
        await self.events.publish(
            "queue_reordered",
            {"task_ids": task_ids, "queue_name": queue_name},
        )
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

    async def preempt_task(self, task_id: int) -> None:
        async with self._lock:
            handle = self._running.get(task_id)
            if handle is None:
                raise ValueError("只有运行中的任务可以抢占")
            urgent_tasks = [
                task
                for task in self.database.list_queue_tasks(URGENT_QUEUE)
                if self._is_task_ready_for_launch(task)
            ]
            if not urgent_tasks:
                raise ValueError("当前没有等待中的紧急任务，请先加入紧急队列")
            handle.stop_reason = "preempt"
            handle.requeue_to_queue_name = NORMAL_QUEUE
            self._signal_process_group(handle.process.pid, signal.SIGTERM)
            asyncio.create_task(
                self._escalate_kill(handle.task_id),
                name=f"preempt-{task_id}",
            )
        await self.events.publish(
            "task_preempting",
            {
                "task_id": task_id,
                "requeue_to_queue_name": NORMAL_QUEUE,
            },
        )

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

    async def subscribe_terminal_stream(
        self,
        task_id: int,
    ) -> tuple[dict[str, object], TerminalSubscriber, bytes]:
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        if task.get("status") != "running":
            raise ValueError("终端流只支持运行中的任务")
        async with self._lock:
            session = self._terminal_sessions.get(task_id)
            if session is None or session.closed:
                raise ValueError("运行中的任务终端不可用")
            subscriber, snapshot = session.subscribe(snapshot_bytes=TERMINAL_SNAPSHOT_BYTES)
        return task, subscriber, snapshot

    async def unsubscribe_terminal_stream(
        self,
        task_id: int,
        subscriber: TerminalSubscriber,
    ) -> None:
        async with self._lock:
            session = self._terminal_sessions.get(task_id)
            if session is None:
                return
            session.unsubscribe(subscriber)

    async def resize_terminal(
        self,
        task_id: int,
        *,
        cols: int,
        rows: int,
    ) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        if task.get("status") != "running":
            raise ValueError("终端尺寸只支持运行中的任务")
        async with self._lock:
            session = self._terminal_sessions.get(task_id)
            if session is None or session.closed:
                raise ValueError("运行中的任务终端不可用")
            try:
                session.resize(cols=cols, rows=rows)
            except OSError as exc:
                raise ValueError("运行中的任务终端不可用") from exc

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
        log_file = open(log_path, "ab")
        master_fd, slave_fd = pty.openpty()
        terminal_cols, terminal_rows = set_terminal_window_size(
            slave_fd,
            cols=DEFAULT_TERMINAL_COLUMNS,
            rows=DEFAULT_TERMINAL_ROWS,
        )
        terminal_session = TerminalSession(
            task_id=task_id,
            master_fd=master_fd,
            log_path=log_path,
            log_file=log_file,
            cols=terminal_cols,
            rows=terminal_rows,
        )
        self._append_terminal_bytes(
            terminal_session,
            (
                f"[exp-scheduler] task={task_id} gpu={gpu_id} started\n"
                f"[exp-scheduler] command={task['command']}\n"
                f"[exp-scheduler] attempt={next_attempt}/{self.config.auto_retry_max_retries + 1}\n"
            ).encode("utf-8"),
        )
        if isinstance(task.get("profile_name"), str) and task["profile_name"]:
            self._append_terminal_bytes(
                terminal_session,
                f"[exp-scheduler] profile={task['profile_name']}\n".encode("utf-8"),
            )

        env = self._build_task_environment(
            task=task,
            gpu_id=gpu_id,
            next_attempt=next_attempt,
            terminal_cols=terminal_session.cols,
            terminal_rows=terminal_session.rows,
        )
        cwd = task["cwd"] if isinstance(task["cwd"], str) and task["cwd"] else None
        launch_command = self._build_launch_command(task)

        try:
            process = subprocess.Popen(
                ["bash", "-lc", launch_command],
                cwd=cwd,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                text=False,
            )
        except Exception as exc:
            self._append_terminal_bytes(
                terminal_session,
                "[exp-scheduler] launch failed\n".encode("utf-8"),
            )
            self._append_terminal_bytes(
                terminal_session,
                "".join(traceback.format_exception(exc)).encode("utf-8", errors="replace"),
            )
            self._close_master_fd(terminal_session)
            log_file.close()
            os.close(slave_fd)
            self.database.mark_task_launch_failed(
                task_id=task_id,
                log_path=str(log_path),
                message=f"启动失败: {exc}",
            )
            await self.events.publish("task_failed_to_launch", {"task_id": task_id})
            return
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass

        running_task = self.database.mark_task_running(
            task_id=task_id,
            gpu_id=gpu_id,
            pid=process.pid,
            log_path=str(log_path),
        )
        terminal_session.reader_task = asyncio.create_task(
            self._read_terminal_output(terminal_session),
            name=f"terminal-reader-{task_id}",
        )
        handle = ProcessHandle(
            task_id=task_id,
            gpu_id=gpu_id,
            process=process,
            log_path=log_path,
            attempt_count=int(running_task.get("attempt_count") or next_attempt),
            terminal_session=terminal_session,
        )
        self._running[task_id] = handle
        self._terminal_sessions[task_id] = terminal_session
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
        session = handle.terminal_session
        final_exit_payload: dict[str, object] | None = None
        try:
            exit_code = await asyncio.to_thread(handle.process.wait)
            if session.reader_task is not None:
                await session.reader_task
            if handle.stop_reason == "preempt":
                requeue_queue_name = handle.requeue_to_queue_name or NORMAL_QUEUE
                self._append_terminal_bytes(
                    session,
                    "\n[exp-scheduler] task_preempted=true "
                    f"requeue_to={requeue_queue_name}\n".encode("utf-8"),
                )
                self.database.preempt_running_task_to_queue_head(
                    task_id,
                    queue_name=requeue_queue_name,
                )
                await self.events.publish(
                    "task_preempted",
                    {
                        "task_id": task_id,
                        "requeue_to_queue_name": requeue_queue_name,
                    },
                )
                final_exit_payload = {
                    "task_id": task_id,
                    "status": "preempted",
                    "queue_name": requeue_queue_name,
                }
                return
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
                self._append_terminal_bytes(
                    session,
                    "\n[exp-scheduler] retry_scheduled=true "
                    f"next_retry_at={next_retry_at} "
                    f"attempt={handle.attempt_count}/{self.config.auto_retry_max_retries + 1}\n".encode(
                        "utf-8"
                    ),
                )
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
                final_exit_payload = {
                    "task_id": task_id,
                    "status": "retry_scheduled",
                    "next_retry_at": next_retry_at,
                }
                return

            self._append_terminal_bytes(
                session,
                f"\n[exp-scheduler] task={task_id} finished status={status} exit_code={exit_code}\n".encode(
                    "utf-8"
                ),
            )
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
            final_exit_payload = {
                "task_id": task_id,
                "status": status,
                "exit_code": exit_code,
            }
        finally:
            await self._close_terminal_session(
                session,
                exit_payload=final_exit_payload,
            )
            async with self._lock:
                self._running.pop(task_id, None)
                self._terminal_sessions.pop(task_id, None)
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
            if handle is None or handle.process.poll() is not None:
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

    def _build_task_environment(
        self,
        *,
        task: dict[str, object],
        gpu_id: int,
        next_attempt: int,
        terminal_cols: int = DEFAULT_TERMINAL_COLUMNS,
        terminal_rows: int = DEFAULT_TERMINAL_ROWS,
    ) -> dict[str, str]:
        env = self._sanitize_scheduler_python_env(os.environ.copy())
        env.update({key: str(value) for key, value in dict(task["env"]).items()})
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
        env["EXP_SCHEDULER_ATTEMPT"] = str(next_attempt)
        env["EXP_SCHEDULER_MAX_RETRIES"] = str(self.config.auto_retry_max_retries)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLUMNS", str(terminal_cols))
        env.setdefault("LINES", str(terminal_rows))
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    async def _read_terminal_output(self, session: TerminalSession) -> None:
        while True:
            try:
                data = await asyncio.to_thread(
                    os.read,
                    session.master_fd,
                    TERMINAL_CHUNK_BYTES,
                )
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    break
                LOGGER.warning(
                    "Failed to read PTY output for task %s: %s",
                    session.task_id,
                    exc,
                )
                break
            if not data:
                break
            self._append_terminal_bytes(session, data)

    async def _close_terminal_session(
        self,
        session: TerminalSession,
        *,
        exit_payload: dict[str, object] | None,
    ) -> None:
        if exit_payload is not None and not session.closed:
            session.publish_exit(exit_payload)
        elif exit_payload is None:
            session.closed = True
            for subscriber in list(session.subscribers):
                subscriber.control_queue.put_nowait(("disconnect", None))
            session.subscribers.clear()
        self._close_master_fd(session)
        if session.reader_task is not None and session.reader_task is not asyncio.current_task():
            await asyncio.gather(session.reader_task, return_exceptions=True)
            session.reader_task = None
        if not session.log_file.closed:
            session.log_file.flush()
            session.log_file.close()

    def _append_terminal_bytes(self, session: TerminalSession, data: bytes) -> None:
        session.append_bytes(data)

    def _close_master_fd(self, session: TerminalSession) -> None:
        if session.master_fd < 0:
            return
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        session.master_fd = -1

    def _sanitize_scheduler_python_env(self, env: dict[str, str]) -> dict[str, str]:
        sanitized = dict(env)
        virtual_env = sanitized.pop("VIRTUAL_ENV", None)
        for key in (
            "VIRTUAL_ENV_PROMPT",
            "_OLD_VIRTUAL_PATH",
            "_OLD_VIRTUAL_PYTHONHOME",
        ):
            sanitized.pop(key, None)

        current_python_dir = Path(sys.executable).resolve().parent
        path_entries = [entry for entry in sanitized.get("PATH", "").split(os.pathsep) if entry]
        blocked_entries: set[str] = set()

        if virtual_env:
            virtual_env_bin = str((Path(virtual_env).expanduser() / "bin").resolve())
            blocked_entries.add(virtual_env_bin)
            if current_python_dir.is_relative_to(Path(virtual_env).expanduser().resolve()):
                blocked_entries.add(str(current_python_dir))

        if blocked_entries:
            filtered_entries = []
            for entry in path_entries:
                try:
                    normalized_entry = str(Path(entry).expanduser().resolve())
                except OSError:
                    normalized_entry = entry
                if normalized_entry in blocked_entries:
                    continue
                filtered_entries.append(entry)
            sanitized["PATH"] = os.pathsep.join(filtered_entries)

        return sanitized


def is_retryable_oom_error(exit_code: int, log_path: Path) -> bool:
    if exit_code in {137, 143, -9, -15}:
        return True
    if not log_path.exists():
        return False
    content = read_text(log_path)
    return bool(RETRYABLE_OOM_PATTERN.search(content))
