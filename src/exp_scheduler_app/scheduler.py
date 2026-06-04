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
from uuid import uuid4

from .config import SchedulerConfig
from .database import Database, NORMAL_QUEUE, STAGED_QUEUE, URGENT_QUEUE
from .events import EventBroker
from .gpu import GPUInfo, query_gpus
from .profile_discovery import discover_installed_environments
from .terminal import (
    DEFAULT_TERMINAL_COLUMNS,
    DEFAULT_TERMINAL_ROWS,
    TASK_TERMINAL_SNAPSHOT_BYTES,
    TERMINAL_CHUNK_BYTES,
    TerminalSession,
    TerminalSubscriber,
    compact_progress_log_file,
    encode_terminal_text,
    read_text,
    read_text_tail,
    set_terminal_window_size,
)


LOGGER = logging.getLogger("exp_scheduler")
LOG_TAIL_BYTES = 1024 * 1024
TERMINATE_GRACE_SECONDS = 5
GPU_MEMORY_BUDGET_HEADROOM_MB = 2048
TASK_LOG_NAME_RE = re.compile(r"^task_(?P<task_id>\d+)_attempt_(?P<attempt>\d+)\.log$")
INTERRUPTED_SIGNAL_NUMBERS = {
    int(signal.SIGHUP),
    int(signal.SIGINT),
    int(signal.SIGQUIT),
    int(signal.SIGTERM),
    int(signal.SIGKILL),
}
INTERRUPTED_EXIT_CODES = {
    *{-signal_number for signal_number in INTERRUPTED_SIGNAL_NUMBERS},
    *{128 + signal_number for signal_number in INTERRUPTED_SIGNAL_NUMBERS},
}
RETRYABLE_OOM_PATTERN = re.compile(
    r"out of memory|cuda out of memory|cublas.*alloc|cuda error: out of memory|"
    r"failed to allocate|cuda runtime error|memory allocation|std::bad_alloc|"
    r"nccl.*unhandled system error|device-side assert triggered|resource exhausted|"
    r"cuda error.*launch out of resources|cuda-capable device.*busy or unavailable|"
    r"cudaerrordevicesunavailable|killed|terminated|oom-kill|out of memory: kill process",
    re.IGNORECASE,
)


def queue_display_name(queue_name: str) -> str:
    if queue_name == URGENT_QUEUE:
        return "紧急"
    if queue_name == STAGED_QUEUE:
        return "暂存"
    return "普通"


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
        self._wake_event = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._running: dict[int, ProcessHandle] = {}
        self._terminal_sessions: dict[int, TerminalSession] = {}
        self._watchers: dict[int, asyncio.Task[None]] = {}
        self._last_gpu_payload: list[dict[str, object]] = []
        self._gpu_ready_counts: dict[tuple[int, str], int] = {}
        self._recently_released_gpu_ids: set[int] = set()
        self._disabled_gpu_idle_since: dict[int, datetime] = {}
        self._external_kill_gpu_cooldown_started_at: dict[int, datetime] = {}

    async def startup(self) -> None:
        self.database.init()
        self._load_persisted_scheduler_settings()
        requeued = self.database.requeue_running_tasks_to_queue_head()
        if requeued:
            LOGGER.info("Requeued %s stale running tasks to queue head", requeued)
        self._stop_event.clear()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="scheduler-loop")
        await self._record_operation(
            level="info",
            action="service_started",
            entity_type="scheduler",
            title="调度服务已启动",
            detail=f"服务启动完成，恢复排队任务 {requeued} 个。",
            metadata={"requeued": requeued},
        )
        await self.events.publish("service_started", {"requeued": requeued})

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
        await self._record_operation(
            level="info",
            action="service_stopped",
            entity_type="scheduler",
            title="调度服务已停止",
        )
        await self.events.publish("service_stopped", {})

    async def list_tasks(
        self,
        *,
        history_limit: int = 100,
        history_offset: int = 0,
        history_sort: str = "finished_at",
        history_status: str | None = None,
    ) -> dict[str, object]:
        payload = self.database.list_tasks(
            history_limit=history_limit,
            history_offset=history_offset,
            history_sort=history_sort,
            history_status=history_status,
        )
        for task in payload.get("history", []):
            if isinstance(task, dict):
                task["attempt_logs"] = self._list_task_log_entries(task)
        return payload

    async def list_gpus(self) -> list[dict[str, object]]:
        await self._apply_due_gpu_schedule()
        if self._last_gpu_payload:
            return self._gpu_payload_with_auto_restore_status(self._last_gpu_payload)
        payload = await self._refresh_gpu_payload()
        return self._gpu_payload_with_auto_restore_status(payload)

    async def list_operation_logs(
        self,
        *,
        limit: int = 200,
        level: str | None = None,
        source: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, object]]:
        return self.database.list_operation_logs(
            limit=limit,
            level=level,
            source=source,
            action=action,
            entity_type=entity_type,
            query=query,
        )

    async def clear_operation_logs(self) -> int:
        count = self.database.clear_operation_logs()
        await self.events.publish("operation_logs_cleared", {"count": count})
        return count

    async def set_task_dependencies(
        self, task_id: int, depends_on_ids: list[int]
    ) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        self.database.replace_dependencies(task_id, depends_on_ids)
        normalized_ids = self.database.get_dependency_ids(task_id)
        task = self.database.get_task(task_id) or task
        await self._record_operation(
            level="info",
            action="task_dependencies_updated",
            entity_type="task",
            entity_id=task_id,
            title=f"任务 #{task_id} 依赖已更新",
            detail=f"任务 #{task_id} 的前置依赖更新为: {normalized_ids}",
            metadata=self._task_log_metadata(task, extra={"depends_on": normalized_ids}),
        )
        await self.events.publish(
            "task_dependencies_updated",
            {"task_id": task_id, "depends_on": normalized_ids},
        )
        await self._trigger_immediate_schedule()

    async def get_task_dependencies_info(self, task_id: int) -> dict[str, object]:
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError(f"任务不存在: {task_id}")
        deps = self.database.get_dependencies(task_id)
        dependents = self.database.get_dependents(task_id)
        satisfied = self.database.are_dependencies_satisfied(task_id)
        return {
            "task": task,
            "dependencies": deps,
            "dependents": dependents,
            "dependencies_satisfied": satisfied,
        }

    async def create_task(
        self,
        *,
        name: str | None,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        notes: str | None,
        is_urgent: bool = False,
        queue_name: str | None = None,
        requested_gpu: int | None = None,
        gpu_memory_budget_mb: int | None = None,
        profile_id: int | None = None,
        depends_on_ids: list[int] | None = None,
    ) -> dict[str, object]:
        normalized_requested_gpu = await self._normalize_requested_gpu(requested_gpu)
        normalized_gpu_memory_budget_mb = self._normalize_gpu_memory_budget_mb(
            gpu_memory_budget_mb
        )
        final_queue_name = queue_name or (URGENT_QUEUE if is_urgent else NORMAL_QUEUE)
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
            gpu_memory_budget_mb=normalized_gpu_memory_budget_mb,
            queue_name=final_queue_name,
            profile_id=profile_id,
            profile_name=profile_name,
            shell_setup=shell_setup,
            depends_on_ids=depends_on_ids,
        )
        await self._record_operation(
            level="success",
            action="task_created",
            entity_type="task",
            entity_id=int(task["id"]),
            title=(
                f"任务 #{task['id']} 已加入暂存队列"
                if task.get("queue_name") == STAGED_QUEUE
                else f"任务 #{task['id']} 已加入队列"
            ),
            detail=f"任务 {task['name']} 已加入{queue_display_name(str(task.get('queue_name')))}队列。",
            metadata=self._task_log_metadata(task),
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
        await self._record_operation(
            level="success",
            action="profile_created",
            entity_type="profile",
            entity_id=int(profile["id"]),
            title=f"环境模板 {profile['name']} 已创建",
            metadata=self._profile_log_metadata(profile),
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
        await self._record_operation(
            level="info",
            action="profile_updated",
            entity_type="profile",
            entity_id=int(profile["id"]),
            title=f"环境模板 {profile['name']} 已更新",
            metadata=self._profile_log_metadata(profile),
        )
        await self.events.publish("profile_updated", {"profile_id": profile["id"]})
        return profile

    async def delete_profile(self, profile_id: int) -> None:
        profile = self.database.get_profile(profile_id)
        deleted = self.database.delete_profile(profile_id)
        if not deleted:
            raise ValueError("环境配置不存在")
        await self._record_operation(
            level="warning",
            action="profile_deleted",
            entity_type="profile",
            entity_id=profile_id,
            title=f"环境模板 #{profile_id} 已删除",
            metadata=self._profile_log_metadata(profile) if profile is not None else {"profile_id": profile_id},
        )
        await self.events.publish("profile_deleted", {"profile_id": profile_id})

    async def delete_task(self, task_id: int) -> None:
        async with self._lock:
            task = self.database.delete_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        deleted_log_count = self._delete_task_log_files(task)
        await self._record_operation(
            level="warning",
            action="task_deleted",
            entity_type="task",
            entity_id=task_id,
            title=f"任务 #{task_id} 已删除",
            detail=f"任务 {task.get('name')} 及其日志文件已删除。",
            metadata=self._task_log_metadata(
                task,
                extra={"deleted_log_count": deleted_log_count},
            ),
        )
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
        queue_name: str | None = None,
        requested_gpu: int | None = None,
        gpu_memory_budget_mb: int | None = None,
        profile_id: int | None = None,
        depends_on_ids: list[int] | None = None,
    ) -> dict[str, object]:
        normalized_requested_gpu = await self._normalize_requested_gpu(requested_gpu)
        normalized_gpu_memory_budget_mb = self._normalize_gpu_memory_budget_mb(
            gpu_memory_budget_mb
        )
        final_queue_name = queue_name or (URGENT_QUEUE if is_urgent else NORMAL_QUEUE)
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
        async with self._lock:
            task = self.database.update_queued_task(
                task_id,
                name=final_name,
                command=command,
                cwd=resolved_cwd,
                env=resolved_env,
                notes=notes,
                requested_gpu=normalized_requested_gpu,
                gpu_memory_budget_mb=normalized_gpu_memory_budget_mb,
                queue_name=final_queue_name,
                profile_id=profile_id,
                profile_name=profile_name,
                shell_setup=shell_setup,
                depends_on_ids=depends_on_ids,
            )
        await self._record_operation(
            level="info",
            action="task_updated",
            entity_type="task",
            entity_id=int(task["id"]),
            title=f"任务 #{task['id']} 已更新",
            detail=f"任务 {task['name']} 的运行参数已更新。",
            metadata=self._task_log_metadata(task),
        )
        await self.events.publish("task_updated", {"task_id": task["id"]})
        await self._trigger_immediate_schedule()
        return task

    async def move_task_to_queue(
        self,
        task_id: int,
        *,
        queue_name: str,
    ) -> dict[str, object]:
        async with self._lock:
            task = self.database.move_task_to_queue(task_id, queue_name=queue_name)
        final_queue_name = str(task.get("queue_name") or NORMAL_QUEUE)
        await self._record_operation(
            level="info",
            action="task_moved_to_queue",
            entity_type="task",
            entity_id=int(task["id"]),
            title=f"任务 #{task['id']} 已移到{queue_display_name(final_queue_name)}队列",
            detail=f"任务 {task['name']} 当前位于{queue_display_name(final_queue_name)}队列。",
            metadata=self._task_log_metadata(task, extra={"queue_name": final_queue_name}),
        )
        await self.events.publish(
            "task_moved_to_queue",
            {"task_id": task["id"], "queue_name": final_queue_name},
        )
        if final_queue_name != STAGED_QUEUE:
            await self._trigger_immediate_schedule()
        return task

    async def update_task_metadata(
        self,
        task_id: int,
        *,
        name: str | None,
        notes: str | None,
        update_name: bool = True,
        update_notes: bool = True,
    ) -> dict[str, object]:
        if not update_name and not update_notes:
            raise ValueError("没有可更新的记录字段")
        current = self.database.get_task(task_id)
        if current is None:
            raise ValueError("任务不存在")
        final_name = str(current["name"])
        if update_name:
            final_name = (name or "").strip() or str(current["command"]).strip()[:80]
        final_notes = current["notes"] if isinstance(current["notes"], str) else None
        if update_notes:
            final_notes = notes.strip() if isinstance(notes, str) and notes.strip() else None
        async with self._lock:
            task = self.database.update_task_metadata(
                task_id,
                name=final_name,
                notes=final_notes,
            )
        await self._record_operation(
            level="info",
            action="task_metadata_updated",
            entity_type="task",
            entity_id=int(task["id"]),
            title=f"任务 #{task['id']} 记录信息已更新",
            detail=f"任务 {task['name']} 的名称或备注已更新。",
            metadata=self._task_log_metadata(task),
        )
        await self.events.publish("task_metadata_updated", {"task_id": task["id"]})
        return task

    async def reorder_tasks(
        self,
        task_ids: list[int],
        *,
        queue_name: str = NORMAL_QUEUE,
    ) -> list[dict[str, object]]:
        queue = self.database.reorder_queue(task_ids, queue_name=queue_name)
        await self._record_operation(
            level="info",
            action="queue_reordered",
            entity_type="queue",
            title=f"{queue_display_name(queue_name)}队列已重排",
            detail=f"新的任务顺序: {task_ids}",
            metadata={"queue_name": queue_name, "task_ids": task_ids},
        )
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
            self._start_process_stop_ladder(handle, name=f"cancel-{task_id}")
        task = self.database.get_task(task_id)
        await self._record_operation(
            level="warning",
            action="task_cancelling",
            entity_type="task",
            entity_id=task_id,
            title=f"任务 #{task_id} 正在取消",
            metadata=self._task_log_metadata(task) if task is not None else {"task_id": task_id},
        )
        await self.events.publish("task_cancelling", {"task_id": task_id})

    async def requeue_task(self, task_id: int) -> dict[str, object]:
        task = self.database.clone_task_for_requeue(task_id)
        await self._record_operation(
            level="success",
            action="task_requeued",
            entity_type="task",
            entity_id=int(task["id"]),
            title=f"任务 #{task_id} 已重新入队为 #{task['id']}",
            metadata=self._task_log_metadata(task, extra={"source_task_id": task_id}),
        )
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
            self._start_process_stop_ladder(handle, name=f"preempt-{task_id}")
        task = self.database.get_task(task_id)
        await self._record_operation(
            level="warning",
            action="task_preempting",
            entity_type="task",
            entity_id=task_id,
            title=f"任务 #{task_id} 正在被抢占",
            detail="当前任务会被停止并放回普通队列队首。",
            metadata=self._task_log_metadata(
                task,
                extra={"requeue_to_queue_name": NORMAL_QUEUE},
            ) if task is not None else {"task_id": task_id, "requeue_to_queue_name": NORMAL_QUEUE},
        )
        await self.events.publish(
            "task_preempting",
            {
                "task_id": task_id,
                "requeue_to_queue_name": NORMAL_QUEUE,
            },
        )

    async def interrupt_running_tasks_to_queue_head(
        self,
        *,
        gpu_ids: set[int] | None = None,
    ) -> int:
        async with self._lock:
            handles = [
                handle
                for handle in self._running.values()
                if (gpu_ids is None or handle.gpu_id in gpu_ids)
                and handle.process.poll() is None
            ]
            for handle in handles:
                handle.stop_reason = "interrupt"
                self._start_process_stop_ladder(
                    handle,
                    name=f"interrupt-requeue-{handle.task_id}",
                )
        if handles:
            await self.events.publish(
                "tasks_interrupting_for_requeue",
                {
                    "task_ids": [handle.task_id for handle in handles],
                    "gpu_ids": sorted(gpu_ids) if gpu_ids is not None else None,
                },
            )
            await self._record_operation(
                level="warning",
                action="tasks_interrupting_for_requeue",
                entity_type="queue",
                title="运行中任务正在中断回队列",
                detail=f"正在中断 {len(handles)} 个运行中任务并放回队列。",
                metadata={
                    "task_ids": [handle.task_id for handle in handles],
                    "gpu_ids": sorted(gpu_ids) if gpu_ids is not None else None,
                },
            )
        return len(handles)

    async def set_queue_paused(self, paused: bool) -> bool:
        result = self.database.set_queue_paused(paused)
        await self._record_operation(
            level="warning" if paused else "success",
            action="queue_paused" if paused else "queue_resumed",
            entity_type="queue",
            title="调度队列已暂停" if paused else "调度队列已恢复",
            metadata={"paused": result},
        )
        await self.events.publish(
            "queue_paused" if paused else "queue_resumed",
            {"paused": result},
        )
        return result

    async def get_settings(self) -> dict[str, object]:
        await self._apply_due_gpu_schedule()
        active_leases = self._active_agent_gpu_leases()
        leased_gpu_ids = self._leased_gpu_ids(active_leases)
        known_gpu_ids = await self._known_gpu_ids()
        return {
            "allowed_gpu_ids": self.database.get_allowed_gpu_ids(),
            "effective_allowed_gpu_ids": self._effective_allowed_gpu_ids(
                known_gpu_ids=known_gpu_ids,
                active_leases=active_leases,
            ),
            "leased_gpu_ids": sorted(leased_gpu_ids),
            "agent_gpu_leases": active_leases,
            "gpu_schedule": self.database.get_gpu_schedule(),
        }

    async def get_scheduler_settings(self) -> dict[str, object]:
        return self._scheduler_settings_payload()

    async def list_agent_gpu_leases(
        self,
        *,
        include_inactive: bool = False,
    ) -> list[dict[str, object]]:
        return self.database.list_agent_gpu_leases(include_inactive=include_inactive)

    async def create_agent_gpu_lease(
        self,
        *,
        owner: str,
        gpu_ids: list[int],
        ttl_seconds: float | None,
        stop_running: bool,
        notes: str | None,
    ) -> dict[str, object]:
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("lease owner 不能为空")
        normalized_gpu_ids = await self._normalize_allowed_gpu_ids(gpu_ids)
        if not normalized_gpu_ids:
            raise ValueError("至少需要指定一个 GPU")
        expires_at = self._lease_expires_at(ttl_seconds)
        lease = self.database.create_agent_gpu_lease(
            lease_id=uuid4().hex,
            owner=normalized_owner,
            gpu_ids=normalized_gpu_ids,
            expires_at=expires_at,
            notes=notes.strip() if isinstance(notes, str) and notes.strip() else None,
        )
        self._last_gpu_payload = []
        interrupted = 0
        if stop_running:
            interrupted = await self.interrupt_running_tasks_to_queue_head(
                gpu_ids=set(normalized_gpu_ids),
            )
        await self._trigger_immediate_schedule()
        settings = await self.get_settings()
        await self._record_operation(
            level="warning" if interrupted else "info",
            action="agent_gpu_lease_created",
            entity_type="gpu_lease",
            title=f"Agent GPU lease 已创建: {lease['id']}",
            detail=(
                f"{normalized_owner} 临时占用 GPU {normalized_gpu_ids}，"
                f"中断并回队列任务数: {interrupted}。"
            ),
            metadata={
                "lease": lease,
                "interrupted": interrupted,
                "settings": settings,
            },
        )
        await self.events.publish(
            "agent_gpu_lease_created",
            {"lease": lease, "interrupted": interrupted},
        )
        await self.events.publish("settings_updated", settings)
        return {"lease": lease, "interrupted": interrupted, "settings": settings}

    async def release_agent_gpu_lease(self, lease_id: str) -> dict[str, object]:
        lease = self.database.release_agent_gpu_lease(lease_id)
        if lease is None:
            raise ValueError(f"GPU lease 不存在: {lease_id}")
        self._last_gpu_payload = []
        await self._trigger_immediate_schedule()
        settings = await self.get_settings()
        await self._record_operation(
            level="success",
            action="agent_gpu_lease_released",
            entity_type="gpu_lease",
            title=f"Agent GPU lease 已释放: {lease['id']}",
            metadata={"lease": lease, "settings": settings},
        )
        await self.events.publish("agent_gpu_lease_released", {"lease": lease})
        await self.events.publish("settings_updated", settings)
        return {"lease": lease, "settings": settings}

    async def update_scheduler_settings(
        self,
        *,
        poll_interval_seconds: object,
        gpu_idle_required_checks: object,
        auto_restore_idle_gpu_seconds: object,
        auto_retry_enabled: object | None,
        auto_retry_max_retries: object,
        auto_retry_delay_seconds: object,
        external_kill_gpu_cooldown_seconds: object,
    ) -> dict[str, object]:
        settings = self._normalize_scheduler_settings(
            poll_interval_seconds=poll_interval_seconds,
            gpu_idle_required_checks=gpu_idle_required_checks,
            auto_restore_idle_gpu_seconds=auto_restore_idle_gpu_seconds,
            auto_retry_enabled=auto_retry_enabled,
            auto_retry_max_retries=auto_retry_max_retries,
            auto_retry_delay_seconds=auto_retry_delay_seconds,
            external_kill_gpu_cooldown_seconds=external_kill_gpu_cooldown_seconds,
        )
        self._apply_scheduler_settings(settings)
        self.database.set_scheduler_settings(**settings)
        if self.config.external_kill_gpu_cooldown_seconds <= 0:
            self._external_kill_gpu_cooldown_started_at.clear()
        self._reset_gpu_ready_counts()
        self._wake_scheduler_loop()
        await self._record_operation(
            level="info",
            action="scheduler_settings_updated",
            entity_type="scheduler",
            title="调控器设置已更新",
            detail=(
                f"轮询间隔 {settings['poll_interval_seconds']} 秒，"
                f"连续空闲确认 {settings['gpu_idle_required_checks']} 次，"
                "空闲自动恢复可用 "
                f"{self._format_auto_restore_idle_gpu_seconds(settings['auto_restore_idle_gpu_seconds'])}，"
                "自动重试 "
                f"{self._format_auto_retry_settings(settings)}，"
                "外部 kill 后 GPU 冷却 "
                f"{self._format_external_kill_gpu_cooldown_seconds(settings['external_kill_gpu_cooldown_seconds'])}。"
            ),
            metadata=self._scheduler_settings_payload(),
        )
        await self.events.publish(
            "scheduler_settings_updated",
            self._scheduler_settings_payload(),
        )
        return self._scheduler_settings_payload()

    async def update_settings(
        self,
        *,
        allowed_gpu_ids: list[int] | None,
        stop_running_gpu_ids: list[int] | None = None,
    ) -> dict[str, object]:
        normalized_allowed_gpu_ids = await self._normalize_allowed_gpu_ids(allowed_gpu_ids)
        normalized_stop_gpu_ids: set[int] = set()
        if stop_running_gpu_ids:
            normalized_stop_gpu_ids = await self._normalize_gpu_id_set(stop_running_gpu_ids)
        self.database.set_allowed_gpu_ids(normalized_allowed_gpu_ids)
        self._last_gpu_payload = []
        await self.events.publish(
            "settings_updated",
            {
                "allowed_gpu_ids": normalized_allowed_gpu_ids,
                "gpu_schedule": self.database.get_gpu_schedule(),
            },
        )
        interrupted = 0
        if normalized_stop_gpu_ids:
            interrupted = await self.interrupt_running_tasks_to_queue_head(
                gpu_ids=normalized_stop_gpu_ids,
            )
        await self._trigger_immediate_schedule()
        settings = await self.get_settings()
        settings["interrupted"] = interrupted
        await self._record_operation(
            level="warning" if interrupted else "info",
            action="settings_updated",
            entity_type="gpu",
            title="GPU 调度范围已更新",
            detail=(
                "已切换可调度 GPU，"
                f"中断并回队列的运行中任务数: {interrupted}。"
            ),
            metadata={
                "allowed_gpu_ids": normalized_allowed_gpu_ids,
                "stop_running_gpu_ids": sorted(normalized_stop_gpu_ids),
                "interrupted": interrupted,
                "gpu_schedule": settings.get("gpu_schedule"),
            },
        )
        return settings

    async def schedule_gpu_state(
        self,
        *,
        gpu_id: int,
        action: str,
        run_at: str,
    ) -> dict[str, object]:
        normalized_gpu_id = await self._normalize_gpu_id(gpu_id)
        normalized_action = self._normalize_gpu_schedule_action(action)
        normalized_run_at = self._normalize_gpu_schedule_time(run_at)
        schedule = self.database.set_gpu_schedule_entry(
            normalized_gpu_id,
            action=normalized_action,
            run_at=normalized_run_at.isoformat(),
        )
        await self.events.publish(
            "gpu_schedule_updated",
            {"gpu_id": normalized_gpu_id, "gpu_schedule": schedule},
        )
        await self._record_operation(
            level="info",
            action="gpu_schedule_updated",
            entity_type="gpu",
            entity_id=normalized_gpu_id,
            title=f"GPU {normalized_gpu_id} 定时计划已设置",
            detail=f"GPU {normalized_gpu_id} 将在 {normalized_run_at.isoformat()} 执行 {normalized_action}。",
            metadata={
                "gpu_id": normalized_gpu_id,
                "action": normalized_action,
                "run_at": normalized_run_at.isoformat(),
                "gpu_schedule": schedule,
            },
        )
        await self._trigger_immediate_schedule()
        return await self.get_settings()

    async def clear_gpu_schedule(self, gpu_id: int) -> dict[str, object]:
        normalized_gpu_id = await self._normalize_gpu_id(gpu_id)
        schedule = self.database.clear_gpu_schedule_entry(normalized_gpu_id)
        await self.events.publish(
            "gpu_schedule_updated",
            {"gpu_id": normalized_gpu_id, "gpu_schedule": schedule},
        )
        await self._record_operation(
            level="info",
            action="gpu_schedule_cleared",
            entity_type="gpu",
            entity_id=normalized_gpu_id,
            title=f"GPU {normalized_gpu_id} 定时计划已清除",
            metadata={"gpu_id": normalized_gpu_id, "gpu_schedule": schedule},
        )
        return await self.get_settings()

    async def list_task_logs(self, task_id: int) -> dict[str, object]:
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        return {"task": task, "logs": self._list_task_log_entries(task)}

    async def read_task_log(
        self,
        task_id: int,
        *,
        attempt: int | None = None,
        tail_bytes: int | None = LOG_TAIL_BYTES,
    ) -> dict[str, object]:
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        logs = self._list_task_log_entries(task)
        selected_log = self._select_task_log_entry(
            task=task,
            logs=logs,
            attempt=attempt,
        )
        content = ""
        if selected_log is not None:
            content = read_text_tail(Path(str(selected_log["path"])), tail_bytes=tail_bytes)
        return {
            "task": task,
            "content": content,
            "logs": logs,
            "log": selected_log,
            "selected_attempt": selected_log["attempt"] if selected_log is not None else None,
        }

    async def delete_task_log(self, task_id: int, *, attempt: int) -> dict[str, object]:
        if attempt < 1:
            raise ValueError("日志尝试次数无效")
        task = self.database.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        logs = self._list_task_log_entries(task)
        selected_log = self._select_task_log_entry(
            task=task,
            logs=logs,
            attempt=attempt,
        )
        if selected_log is None:
            raise ValueError("日志不存在")
        if task.get("status") == "running" and bool(selected_log.get("is_current")):
            raise ValueError("运行中的当前日志不能删除")
        log_path = Path(str(selected_log["path"]))
        try:
            log_path.unlink()
        except FileNotFoundError:
            raise ValueError("日志不存在") from None
        except OSError as exc:
            raise ValueError(f"日志删除失败: {exc}") from exc

        remaining_logs = self._list_task_log_entries(task)
        await self._record_operation(
            level="warning",
            action="task_log_deleted",
            entity_type="task",
            entity_id=task_id,
            title=f"任务 #{task_id} 第 {attempt} 次运行日志已删除",
            detail=f"已删除日志文件: {log_path}",
            metadata=self._task_log_metadata(
                task,
                extra={
                    "log_attempt": attempt,
                    "deleted_log_path": str(log_path),
                    "deleted_log_size_bytes": selected_log.get("size_bytes"),
                },
            ),
        )
        await self.events.publish(
            "task_log_deleted",
            {"task_id": task_id, "attempt": attempt},
        )
        return {"task": task, "deleted_log": selected_log, "logs": remaining_logs}

    async def subscribe_terminal_stream(
        self,
        task_id: int,
        *,
        full_snapshot: bool = False,
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
            subscriber, snapshot = session.subscribe(
                snapshot_bytes=None if full_snapshot else TASK_TERMINAL_SNAPSHOT_BYTES
            )
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
                    self._wait_for_next_tick(),
                    timeout=self.config.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def _wait_for_next_tick(self) -> None:
        stop_task = asyncio.create_task(self._stop_event.wait())
        wake_task = asyncio.create_task(self._wake_event.wait())
        tasks = {stop_task, wake_task}
        try:
            await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self._wake_event.clear()

    async def _tick(self) -> None:
        await self._apply_due_gpu_schedule()
        payload = await self._refresh_gpu_payload()
        restored_gpu_ids = await self._apply_auto_restore_idle_gpus(payload)
        if restored_gpu_ids:
            payload = await self._refresh_gpu_payload()
        async with self._lock:
            queued_tasks = [
                task
                for task in self.database.list_queued_tasks()
                if self._is_task_ready_for_launch(task)
            ]
            self._refresh_gpu_ready_counts(payload, queued_tasks)
            if self.database.get_queue_paused():
                return
            if not queued_tasks:
                return
            available = [
                gpu
                for gpu in payload
                if gpu["globally_enabled"]
                and not gpu["scheduler_occupied"]
                and not self._is_gpu_in_external_kill_cooldown(int(gpu["index"]))
            ]
            if not available:
                return
            for task, gpu in self._match_tasks_to_gpus(queued_tasks, available):
                await self._launch_task(task, gpu)


    async def _apply_due_gpu_schedule(self) -> None:
        schedule = self.database.get_gpu_schedule()
        if not schedule:
            return
        now = datetime.now(UTC)
        allowed_gpu_ids = self.database.get_allowed_gpu_ids()
        known_gpu_ids = await self._known_gpu_ids()
        enabled = set(known_gpu_ids if allowed_gpu_ids is None else allowed_gpu_ids)
        changed = False
        remaining: dict[str, dict[str, str | int]] = {}
        for key, entry in schedule.items():
            try:
                gpu_id = int(key)
                run_at = datetime.fromisoformat(str(entry["run_at"]))
            except (KeyError, TypeError, ValueError):
                changed = True
                continue
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=UTC)
            if run_at > now:
                remaining[key] = entry
                continue
            action = entry.get("action")
            if action == "enable":
                enabled.add(gpu_id)
            elif action == "disable":
                enabled.discard(gpu_id)
            changed = True
        if not changed:
            return
        normalized_allowed_gpu_ids: list[int] | None
        if enabled == known_gpu_ids:
            normalized_allowed_gpu_ids = None
        else:
            normalized_allowed_gpu_ids = sorted(enabled)
        self.database.set_allowed_gpu_ids(normalized_allowed_gpu_ids)
        self.database.set_gpu_schedule(remaining)
        self._last_gpu_payload = []
        await self._record_operation(
            level="info",
            action="gpu_schedule_applied",
            entity_type="gpu",
            title="GPU 定时计划已执行",
            detail=f"当前可调度 GPU: {normalized_allowed_gpu_ids if normalized_allowed_gpu_ids is not None else '全部'}。",
            metadata={
                "allowed_gpu_ids": normalized_allowed_gpu_ids,
                "gpu_schedule": remaining,
            },
        )
        await self.events.publish(
            "gpu_schedule_applied",
            {
                "allowed_gpu_ids": normalized_allowed_gpu_ids,
                "gpu_schedule": remaining,
            },
        )

    async def _apply_auto_restore_idle_gpus(
        self,
        gpu_payload: list[dict[str, object]],
    ) -> list[int]:
        restore_after_seconds = self.config.auto_restore_idle_gpu_seconds
        now = datetime.now(UTC)
        disabled_gpu_ids = self._sync_auto_restore_idle_tracking(gpu_payload, now=now)
        if not disabled_gpu_ids:
            return []

        known_gpu_ids = {int(gpu["index"]) for gpu in gpu_payload}
        allowed_gpu_ids = self.database.get_allowed_gpu_ids()
        enabled_gpu_ids = {gpu_id for gpu_id in (allowed_gpu_ids or []) if gpu_id in known_gpu_ids}
        leased_gpu_ids = self._leased_gpu_ids() & known_gpu_ids
        restored_gpu_ids: list[int] = []
        for gpu in gpu_payload:
            gpu_id = int(gpu["index"])
            if gpu_id not in disabled_gpu_ids or gpu_id in leased_gpu_ids:
                continue
            idle_since = self._disabled_gpu_idle_since.get(gpu_id)
            if idle_since is None:
                continue
            idle_seconds = (now - idle_since).total_seconds()
            if idle_seconds >= float(restore_after_seconds):
                restored_gpu_ids.append(gpu_id)

        if not restored_gpu_ids:
            return []

        restored_set = set(restored_gpu_ids)
        enabled_gpu_ids.update(restored_set)
        normalized_allowed_gpu_ids: list[int] | None
        if enabled_gpu_ids == known_gpu_ids:
            normalized_allowed_gpu_ids = None
        else:
            normalized_allowed_gpu_ids = sorted(enabled_gpu_ids)
        self.database.set_allowed_gpu_ids(normalized_allowed_gpu_ids)
        for gpu_id in restored_set:
            self._disabled_gpu_idle_since.pop(gpu_id, None)
        self._last_gpu_payload = []
        await self._record_operation(
            level="success",
            action="gpu_auto_restored",
            entity_type="gpu",
            title="GPU 已按空闲策略自动恢复可用",
            detail=(
                f"GPU {sorted(restored_set)} 已连续空闲 "
                f"{restore_after_seconds:g} 秒，恢复到全局可用列表。"
            ),
            metadata={
                "gpu_ids": sorted(restored_set),
                "allowed_gpu_ids": normalized_allowed_gpu_ids,
                "auto_restore_idle_gpu_seconds": restore_after_seconds,
            },
        )
        payload = {
            "gpu_ids": sorted(restored_set),
            "allowed_gpu_ids": normalized_allowed_gpu_ids,
            "gpu_schedule": self.database.get_gpu_schedule(),
        }
        await self.events.publish("gpu_auto_restored", payload)
        await self.events.publish("settings_updated", payload)
        return sorted(restored_set)

    async def _refresh_gpu_payload(self) -> list[dict[str, object]]:
        now = datetime.now(UTC)
        self._prune_external_kill_gpu_cooldowns(now=now)
        gpus = await asyncio.to_thread(self._gpu_provider)
        occupied = {handle.gpu_id for handle in self._running.values()}
        known_gpu_ids = {gpu.index for gpu in gpus}
        active_leases = self._active_agent_gpu_leases()
        leases_by_gpu = self._agent_leases_by_gpu(active_leases)
        allowed_gpu_set = self._effective_allowed_gpu_set(
            known_gpu_ids=known_gpu_ids,
            active_leases=active_leases,
        )
        payload: list[dict[str, object]] = []
        for gpu in gpus:
            entry = gpu.to_dict(
                threshold_mb=self.config.gpu_idle_memory_mb,
                scheduler_occupied=gpu.index in occupied,
                globally_enabled=allowed_gpu_set is None or gpu.index in allowed_gpu_set,
            )
            gpu_leases = leases_by_gpu.get(gpu.index, [])
            if gpu_leases:
                entry["leased_by_agent"] = True
                entry["agent_leases"] = [
                    {
                        "id": lease["id"],
                        "owner": lease["owner"],
                        "expires_at": lease["expires_at"],
                    }
                    for lease in gpu_leases
                ]
            else:
                entry["leased_by_agent"] = False
                entry["agent_leases"] = []
            cooldown_until = self._gpu_external_kill_cooldown_until(
                gpu.index,
                now=now,
            )
            if cooldown_until is not None:
                remaining_seconds = max(
                    0,
                    int((cooldown_until - now).total_seconds() + 0.999),
                )
                entry["is_idle"] = False
                entry["cooldown_until"] = cooldown_until.isoformat()
                entry["cooldown_remaining_seconds"] = remaining_seconds
                entry["cooldown_reason"] = "external_signal"
            payload.append(entry)
        self._sync_auto_restore_idle_tracking(payload, now=now)
        self._add_auto_restore_idle_status(payload, now=now, include_elapsed=False)
        if payload != self._last_gpu_payload:
            self._last_gpu_payload = payload
            await self.events.publish("gpu_updated", {"gpus": payload})
        return payload

    def _sync_auto_restore_idle_tracking(
        self,
        gpu_payload: list[dict[str, object]],
        *,
        now: datetime,
    ) -> set[int]:
        restore_after_seconds = self.config.auto_restore_idle_gpu_seconds
        if restore_after_seconds is None or restore_after_seconds <= 0:
            self._disabled_gpu_idle_since.clear()
            return set()
        known_gpu_ids = {int(gpu["index"]) for gpu in gpu_payload}
        allowed_gpu_ids = self.database.get_allowed_gpu_ids()
        if allowed_gpu_ids is None:
            self._disabled_gpu_idle_since.clear()
            return set()
        enabled_gpu_ids = {gpu_id for gpu_id in allowed_gpu_ids if gpu_id in known_gpu_ids}
        leased_gpu_ids = self._leased_gpu_ids() & known_gpu_ids
        disabled_gpu_ids = (known_gpu_ids - enabled_gpu_ids) - leased_gpu_ids
        if not disabled_gpu_ids:
            self._disabled_gpu_idle_since.clear()
            return set()

        for gpu in gpu_payload:
            gpu_id = int(gpu["index"])
            if gpu_id not in disabled_gpu_ids:
                continue
            if self._is_gpu_physically_idle(gpu):
                self._disabled_gpu_idle_since.setdefault(gpu_id, now)
            else:
                self._disabled_gpu_idle_since.pop(gpu_id, None)

        for gpu_id in set(self._disabled_gpu_idle_since) - disabled_gpu_ids:
            self._disabled_gpu_idle_since.pop(gpu_id, None)
        return disabled_gpu_ids

    def _gpu_payload_with_auto_restore_status(
        self,
        gpu_payload: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        now = datetime.now(UTC)
        payload = [
            {
                key: value
                for key, value in gpu.items()
                if not str(key).startswith("auto_restore_idle_")
            }
            for gpu in gpu_payload
        ]
        self._sync_auto_restore_idle_tracking(payload, now=now)
        self._add_auto_restore_idle_status(payload, now=now, include_elapsed=True)
        return payload

    def _add_auto_restore_idle_status(
        self,
        gpu_payload: list[dict[str, object]],
        *,
        now: datetime,
        include_elapsed: bool,
    ) -> None:
        restore_after_seconds = self.config.auto_restore_idle_gpu_seconds
        if restore_after_seconds is None or restore_after_seconds <= 0:
            return
        known_gpu_ids = {int(gpu["index"]) for gpu in gpu_payload}
        allowed_gpu_ids = self.database.get_allowed_gpu_ids()
        if allowed_gpu_ids is None:
            return
        enabled_gpu_ids = {gpu_id for gpu_id in allowed_gpu_ids if gpu_id in known_gpu_ids}
        leased_gpu_ids = self._leased_gpu_ids() & known_gpu_ids
        disabled_gpu_ids = (known_gpu_ids - enabled_gpu_ids) - leased_gpu_ids
        for gpu in gpu_payload:
            gpu_id = int(gpu["index"])
            if gpu_id not in disabled_gpu_ids:
                continue
            gpu["auto_restore_idle_required_seconds"] = float(restore_after_seconds)
            idle_since = self._disabled_gpu_idle_since.get(gpu_id)
            is_waiting = idle_since is not None and self._is_gpu_physically_idle(gpu)
            gpu["auto_restore_idle_waiting"] = is_waiting
            if not is_waiting:
                if include_elapsed:
                    gpu["auto_restore_idle_wait_seconds"] = 0.0
                    gpu["auto_restore_idle_remaining_seconds"] = float(restore_after_seconds)
                continue
            idle_seconds = max(0.0, (now - idle_since).total_seconds())
            gpu["auto_restore_idle_since"] = idle_since.isoformat()
            if include_elapsed:
                gpu["auto_restore_idle_wait_seconds"] = idle_seconds
                gpu["auto_restore_idle_remaining_seconds"] = max(
                    0.0,
                    float(restore_after_seconds) - idle_seconds,
                )

    async def _launch_task(
        self,
        task: dict[str, object],
        gpu: dict[str, object],
    ) -> None:
        task_id = int(task["id"])
        gpu_id = int(gpu["index"])
        self._clear_gpu_ready_counts(gpu_id)
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
            encode_terminal_text(
                f"[exp-scheduler] task={task_id} gpu={gpu_id} started\n"
                f"[exp-scheduler] command={task['command']}\n"
                f"[exp-scheduler] attempt={next_attempt}/{self.config.auto_retry_max_retries + 1}\n"
            ),
        )
        if isinstance(task.get("profile_name"), str) and task["profile_name"]:
            self._append_terminal_bytes(
                terminal_session,
                encode_terminal_text(f"[exp-scheduler] profile={task['profile_name']}\n"),
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
                encode_terminal_text("[exp-scheduler] launch failed\n"),
            )
            self._append_terminal_bytes(
                terminal_session,
                encode_terminal_text("".join(traceback.format_exception(exc))),
            )
            self._close_master_fd(terminal_session)
            log_file.close()
            os.close(slave_fd)
            failed_task = self.database.mark_task_launch_failed(
                task_id=task_id,
                log_path=str(log_path),
                message=f"启动失败: {exc}",
            )
            await self._record_operation(
                level="error",
                action="task_failed_to_launch",
                entity_type="task",
                entity_id=task_id,
                title=f"任务 #{task_id} 启动失败",
                detail=str(exc),
                metadata=self._task_log_metadata(
                    failed_task,
                    extra={
                        "gpu_id": gpu_id,
                        "attempt": next_attempt,
                        "launch_command": launch_command,
                        "launch_env": env,
                        "exception": "".join(traceback.format_exception(exc)),
                    },
                ),
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
        await self._record_operation(
            level="success",
            action="task_started",
            entity_type="task",
            entity_id=task_id,
            title=f"任务 #{task_id} 已启动",
            detail=f"任务 {running_task['name']} 已在 GPU {gpu_id} 启动，PID {running_task['pid']}。",
            metadata=self._task_log_metadata(
                running_task,
                extra={
                    "gpu_id": gpu_id,
                    "pid": running_task["pid"],
                    "attempt": next_attempt,
                    "launch_command": launch_command,
                    "launch_env": env,
                },
            ),
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
                    encode_terminal_text(
                        "\n[exp-scheduler] task_preempted=true "
                        f"requeue_to={requeue_queue_name}\n"
                    ),
                )
                self.database.preempt_running_task_to_queue_head(
                    task_id,
                    queue_name=requeue_queue_name,
                )
                task = self.database.get_task(task_id)
                await self._record_operation(
                    level="warning",
                    action="task_preempted",
                    entity_type="task",
                    entity_id=task_id,
                    title=f"任务 #{task_id} 已被抢占并回队列",
                    metadata=self._task_log_metadata(
                        task,
                        extra={"requeue_to_queue_name": requeue_queue_name, "exit_code": exit_code},
                    ) if task is not None else {
                        "task_id": task_id,
                        "requeue_to_queue_name": requeue_queue_name,
                        "exit_code": exit_code,
                    },
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
                await self._requeue_interrupted_task(
                    handle=handle,
                    session=session,
                    exit_code=exit_code,
                    reason="scheduler_interrupted",
                )
                final_exit_payload = {
                    "task_id": task_id,
                    "status": "interrupted_requeued",
                    "exit_code": exit_code,
                }
                return
            elif self._is_interrupted_exit(exit_code):
                await self._requeue_interrupted_task(
                    handle=handle,
                    session=session,
                    exit_code=exit_code,
                    reason="signal_interrupted",
                )
                final_exit_payload = {
                    "task_id": task_id,
                    "status": "interrupted_requeued",
                    "exit_code": exit_code,
                }
                return
            else:
                status = "succeeded" if exit_code == 0 else "failed"

            if (
                status == "failed"
                and self._should_retry_task(handle=handle, exit_code=exit_code)
            ):
                next_retry_at = self._next_retry_at()
                self._append_terminal_bytes(
                    session,
                    encode_terminal_text(
                        "\n[exp-scheduler] retry_scheduled=true "
                        f"next_retry_at={next_retry_at} "
                        f"attempt={handle.attempt_count}/{self.config.auto_retry_max_retries + 1}\n"
                    ),
                )
                self.database.schedule_task_retry(
                    task_id=task_id,
                    next_retry_at=next_retry_at,
                    exit_code=exit_code,
                )
                task = self.database.get_task(task_id)
                await self._record_operation(
                    level="warning",
                    action="task_retry_scheduled",
                    entity_type="task",
                    entity_id=task_id,
                    title=f"任务 #{task_id} 已安排自动重试",
                    detail=f"退出码 {exit_code}，下一次重试时间 {next_retry_at}。",
                    metadata=self._task_log_metadata(
                        task,
                        extra={
                            "attempt_count": handle.attempt_count,
                            "max_retries": self.config.auto_retry_max_retries,
                            "next_retry_at": next_retry_at,
                            "exit_code": exit_code,
                        },
                    ) if task is not None else {
                        "task_id": task_id,
                        "attempt_count": handle.attempt_count,
                        "max_retries": self.config.auto_retry_max_retries,
                        "next_retry_at": next_retry_at,
                        "exit_code": exit_code,
                    },
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
                encode_terminal_text(
                    f"\n[exp-scheduler] task={task_id} finished status={status} exit_code={exit_code}\n"
                ),
            )
            finished_task = self.database.finish_task(
                task_id=task_id,
                status=status,
                exit_code=exit_code,
                pid=None,
            )
            await self._record_operation(
                level="success" if status == "succeeded" else "error",
                action="task_finished",
                entity_type="task",
                entity_id=task_id,
                title=f"任务 #{task_id} {'完成' if status == 'succeeded' else '失败'}",
                detail=f"任务退出状态 {status}，退出码 {exit_code}。",
                metadata=self._task_log_metadata(
                    finished_task,
                    extra={"status": status, "exit_code": exit_code},
                ),
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
                self._mark_gpu_recently_released(handle.gpu_id)
            await self._trigger_immediate_schedule()

    async def _requeue_interrupted_task(
        self,
        *,
        handle: ProcessHandle,
        session: TerminalSession,
        exit_code: int,
        reason: str,
    ) -> None:
        task_id = handle.task_id
        cooldown_metadata: dict[str, object] = {}
        if reason == "signal_interrupted":
            cooldown_metadata = self._start_external_kill_gpu_cooldown(
                gpu_id=handle.gpu_id,
                task_id=task_id,
                exit_code=exit_code,
            )
        self._append_terminal_bytes(
            session,
            encode_terminal_text(
                "\n[exp-scheduler] interrupted_requeued=true "
                f"reason={reason} exit_code={exit_code}\n"
            ),
        )
        if cooldown_metadata:
            self._append_terminal_bytes(
                session,
                encode_terminal_text(
                    "[exp-scheduler] external_kill_gpu_cooldown=true "
                    f"gpu={handle.gpu_id} "
                    f"seconds={cooldown_metadata['cooldown_seconds']} "
                    f"until={cooldown_metadata['cooldown_until']}\n"
                ),
            )
        task = self.database.requeue_running_task_to_queue_head(
            task_id,
            exit_code=exit_code,
        )
        detail = f"原因: {reason}，退出码: {exit_code}。"
        if cooldown_metadata:
            detail += (
                f" GPU {handle.gpu_id} 冷却 "
                f"{cooldown_metadata['cooldown_seconds']:g} 秒。"
            )
        await self._record_operation(
            level="warning",
            action="task_interrupted_requeued",
            entity_type="task",
            entity_id=task_id,
            title=f"任务 #{task_id} 已中断并回队列",
            detail=detail,
            metadata=self._task_log_metadata(
                task,
                extra={
                    "reason": reason,
                    "exit_code": exit_code,
                    **cooldown_metadata,
                },
            ),
        )
        await self.events.publish(
            "task_interrupted_requeued",
            {
                "task_id": task_id,
                "reason": reason,
                "exit_code": exit_code,
                **cooldown_metadata,
            },
        )

    def _is_interrupted_exit(self, exit_code: int) -> bool:
        return exit_code in INTERRUPTED_EXIT_CODES

    def _refresh_gpu_ready_counts(
        self,
        gpu_payload: list[dict[str, object]],
        queued_tasks: list[dict[str, object]],
    ) -> None:
        budget_values: set[int] = set()
        for task in queued_tasks:
            budget_mb = task.get("gpu_memory_budget_mb")
            if isinstance(budget_mb, int):
                budget_values.add(budget_mb)
        satisfied_keys: set[tuple[int, str]] = set()
        observed_recently_released_gpu_ids: set[int] = set()
        recently_released_gpu_ids = set(self._recently_released_gpu_ids)
        now = datetime.now(UTC)
        for gpu in gpu_payload:
            gpu_id = int(gpu["index"])
            if self._is_gpu_in_external_kill_cooldown(gpu_id, now=now):
                continue
            if gpu_id in recently_released_gpu_ids and not bool(gpu.get("scheduler_occupied")):
                observed_recently_released_gpu_ids.add(gpu_id)
            if not bool(gpu.get("globally_enabled")) or bool(gpu.get("scheduler_occupied")):
                continue
            if bool(gpu.get("is_idle")):
                satisfied_keys.add((gpu_id, "idle"))
            for budget_mb in budget_values:
                if self._gpu_has_budget_capacity(gpu, budget_mb):
                    satisfied_keys.add((gpu_id, self._budget_readiness_key(budget_mb)))
        previous_counts = self._gpu_ready_counts
        required_checks = self._required_gpu_ready_checks()
        refreshed_counts: dict[tuple[int, str], int] = {}
        for key in satisfied_keys:
            count = previous_counts.get(key, 0) + 1
            if key[0] in recently_released_gpu_ids:
                count = max(count, required_checks)
            refreshed_counts[key] = count
        self._gpu_ready_counts = refreshed_counts
        self._recently_released_gpu_ids.difference_update(observed_recently_released_gpu_ids)

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
                    if int(gpu["index"]) == requested_gpu and self._can_task_run_on_gpu(task, gpu):
                        chosen_index = index
                        break
            else:
                for index, gpu in enumerate(remaining_gpus):
                    if self._can_task_run_on_gpu(task, gpu):
                        chosen_index = index
                        break
            if chosen_index is None:
                continue
            assignments.append((task, remaining_gpus.pop(chosen_index)))
            if not remaining_gpus:
                break
        return assignments

    def _can_task_run_on_gpu(
        self,
        task: dict[str, object],
        gpu: dict[str, object],
    ) -> bool:
        key = self._readiness_key_for_task(task, gpu)
        if key is None:
            return False
        return self._gpu_ready_counts.get(key, 0) >= self._required_gpu_ready_checks()

    def _readiness_key_for_task(
        self,
        task: dict[str, object],
        gpu: dict[str, object],
    ) -> tuple[int, str] | None:
        gpu_id = int(gpu["index"])
        if (
            not bool(gpu.get("globally_enabled"))
            or bool(gpu.get("scheduler_occupied"))
            or self._is_gpu_in_external_kill_cooldown(gpu_id)
        ):
            return None
        budget_mb = task.get("gpu_memory_budget_mb")
        if isinstance(budget_mb, int):
            if self._gpu_has_budget_capacity(gpu, budget_mb):
                return (gpu_id, self._budget_readiness_key(budget_mb))
            return None
        if bool(gpu.get("is_idle")):
            return (gpu_id, "idle")
        return None

    def _gpu_has_budget_capacity(self, gpu: dict[str, object], budget_mb: int) -> bool:
        free_mb = int(gpu.get("memory_total_mb") or 0) - int(gpu.get("memory_used_mb") or 0)
        return free_mb > budget_mb + GPU_MEMORY_BUDGET_HEADROOM_MB

    def _is_gpu_physically_idle(self, gpu: dict[str, object]) -> bool:
        if "physically_idle" in gpu:
            return bool(gpu.get("physically_idle"))
        return (
            int(gpu.get("memory_used_mb") or 0) < self.config.gpu_idle_memory_mb
            and not bool(gpu.get("has_processes"))
            and not bool(gpu.get("scheduler_occupied"))
        )

    def _budget_readiness_key(self, budget_mb: int) -> str:
        return f"budget:{budget_mb}"

    def _required_gpu_ready_checks(self) -> int:
        return max(1, int(self.config.gpu_idle_required_checks))

    async def _interrupt_running_tasks(self) -> None:
        async with self._lock:
            handles = list(self._running.values())
            for handle in handles:
                handle.stop_reason = "interrupt"
            self._signal_live_process_groups(handles, signal.SIGINT)
        if handles:
            await asyncio.sleep(TERMINATE_GRACE_SECONDS)
            self._signal_live_process_groups(handles, signal.SIGTERM)
            await asyncio.sleep(TERMINATE_GRACE_SECONDS)
            self._signal_live_process_groups(handles, signal.SIGKILL)

    def _start_process_stop_ladder(self, handle: ProcessHandle, *, name: str) -> None:
        self._signal_process_group(handle.process.pid, signal.SIGINT)
        asyncio.create_task(self._escalate_process_stop(handle.task_id), name=name)

    async def _escalate_process_stop(self, task_id: int) -> None:
        await asyncio.sleep(TERMINATE_GRACE_SECONDS)
        async with self._lock:
            handle = self._running.get(task_id)
            if handle is None or handle.process.poll() is not None:
                return
            self._signal_process_group(handle.process.pid, signal.SIGTERM)
        await asyncio.sleep(TERMINATE_GRACE_SECONDS)
        async with self._lock:
            handle = self._running.get(task_id)
            if handle is None or handle.process.poll() is not None:
                return
            self._signal_process_group(handle.process.pid, signal.SIGKILL)

    def _signal_live_process_groups(
        self,
        handles: list[ProcessHandle],
        sig: signal.Signals,
    ) -> None:
        for handle in handles:
            if handle.process.poll() is None:
                self._signal_process_group(handle.process.pid, sig)

    def _signal_process_group(self, pid: int | None, sig: signal.Signals) -> None:
        if pid is None:
            return
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return

    def _log_path_for_task(self, task_id: int, attempt_count: int) -> Path:
        return self.config.log_dir / f"task_{task_id}_attempt_{attempt_count}.log"

    def _list_task_log_entries(self, task: dict[str, object]) -> list[dict[str, object]]:
        task_id = int(task["id"])
        entries_by_attempt: dict[int, dict[str, object]] = {}
        attempts_by_number = self._task_attempt_metadata(task)
        for path in self.config.log_dir.glob(f"task_{task_id}_attempt_*.log"):
            entry = self._task_log_entry_from_path(task, path)
            if entry is not None:
                self._apply_attempt_metadata(
                    entry,
                    attempts_by_number.get(int(entry["attempt"])),
                    task=task,
                )
                entries_by_attempt[int(entry["attempt"])] = entry

        log_path = task.get("log_path")
        if isinstance(log_path, str) and log_path:
            entry = self._task_log_entry_from_path(task, Path(log_path))
            if entry is not None:
                self._apply_attempt_metadata(
                    entry,
                    attempts_by_number.get(int(entry["attempt"])),
                    task=task,
                )
                entries_by_attempt[int(entry["attempt"])] = entry

        return [entries_by_attempt[attempt] for attempt in sorted(entries_by_attempt)]

    def _task_attempt_metadata(
        self,
        task: dict[str, object],
    ) -> dict[int, dict[str, object]]:
        task_id = int(task["id"])
        records: dict[int, dict[str, object]] = {}

        for attempt_row in self.database.list_task_attempts(task_id):
            attempt = self._positive_int(attempt_row.get("attempt"))
            if attempt is None:
                continue
            records[attempt] = {
                "attempt": attempt,
                "started_at": attempt_row.get("started_at"),
                "finished_at": attempt_row.get("finished_at"),
                "status": attempt_row.get("status"),
                "exit_code": attempt_row.get("exit_code"),
                "log_path": attempt_row.get("log_path"),
            }

        for operation_log in self.database.list_task_operation_logs(task_id):
            metadata = operation_log.get("metadata")
            if not isinstance(metadata, dict):
                continue
            attempt = self._positive_int(metadata.get("attempt"))
            if attempt is None:
                attempt = self._positive_int(metadata.get("attempt_count"))
            if attempt is None:
                continue
            record = records.setdefault(attempt, {"attempt": attempt})
            action = str(operation_log.get("action") or "")
            created_at = operation_log.get("created_at")
            if action == "task_started":
                self._set_if_missing(record, "started_at", created_at)
                self._set_if_missing(record, "status", "running")
                self._set_if_missing(record, "exit_code", metadata.get("exit_code"))
                self._set_if_missing(record, "log_path", metadata.get("log_path"))
            elif action in {"task_finished", "task_failed_to_launch"}:
                self._set_if_missing(record, "finished_at", created_at)
                record["status"] = metadata.get("status") or "failed"
                record["exit_code"] = metadata.get("exit_code")
                self._set_if_missing(record, "log_path", metadata.get("log_path"))
            elif action == "task_retry_scheduled":
                self._set_if_missing(record, "finished_at", created_at)
                record["status"] = "retry_scheduled"
                record["exit_code"] = metadata.get("exit_code")
                self._set_if_missing(record, "log_path", metadata.get("log_path"))
            elif action in {"task_interrupted_requeued", "task_preempted"}:
                self._set_if_missing(record, "finished_at", created_at)
                record["status"] = action.replace("task_", "")
                record["exit_code"] = metadata.get("exit_code")
                self._set_if_missing(record, "log_path", metadata.get("log_path"))

        current_attempt = self._positive_int(task.get("attempt_count"))
        if current_attempt is not None:
            record = records.setdefault(current_attempt, {"attempt": current_attempt})
            self._set_if_missing(record, "started_at", task.get("started_at"))
            self._set_if_missing(record, "finished_at", task.get("finished_at"))
            self._set_if_missing(record, "status", task.get("status"))
            self._set_if_missing(record, "exit_code", task.get("exit_code"))
            self._set_if_missing(record, "log_path", task.get("log_path"))

        return records

    def _apply_attempt_metadata(
        self,
        entry: dict[str, object],
        metadata: dict[str, object] | None,
        *,
        task: dict[str, object],
    ) -> None:
        if metadata:
            for key in ("started_at", "finished_at", "status", "exit_code"):
                entry[key] = metadata.get(key)
        else:
            entry["started_at"] = None
            entry["finished_at"] = None
            entry["status"] = None
            entry["exit_code"] = None

        if bool(entry.get("is_current")):
            entry["started_at"] = entry.get("started_at") or task.get("started_at")
            entry["finished_at"] = entry.get("finished_at") or task.get("finished_at")
            entry["status"] = entry.get("status") or task.get("status")
            entry["exit_code"] = entry.get("exit_code") if entry.get("exit_code") is not None else task.get("exit_code")
        entry["finished_at"] = entry.get("finished_at") or entry.get("modified_at")

    def _set_if_missing(
        self,
        record: dict[str, object],
        key: str,
        value: object,
    ) -> None:
        if value is not None and record.get(key) is None:
            record[key] = value

    def _positive_int(self, value: object) -> int | None:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _task_log_entry_from_path(
        self,
        task: dict[str, object],
        path: Path,
    ) -> dict[str, object] | None:
        if not path.exists() or not path.is_file():
            return None
        match = TASK_LOG_NAME_RE.match(path.name)
        if match is None:
            return None
        task_id = int(task["id"])
        if int(match.group("task_id")) != task_id:
            return None
        stat = path.stat()
        current_path = task.get("log_path")
        is_current = False
        if isinstance(current_path, str) and current_path:
            is_current = Path(current_path).expanduser().resolve() == path.expanduser().resolve()
        return {
            "attempt": int(match.group("attempt")),
            "path": str(path),
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            "is_current": is_current,
        }

    def _select_task_log_entry(
        self,
        *,
        task: dict[str, object],
        logs: list[dict[str, object]],
        attempt: int | None,
    ) -> dict[str, object] | None:
        if attempt is not None:
            if attempt < 1:
                raise ValueError("日志尝试次数无效")
            for log in logs:
                if int(log["attempt"]) == attempt:
                    return log
            raise ValueError("日志不存在")

        for log in logs:
            if bool(log.get("is_current")):
                return log
        if logs:
            return logs[-1]
        return None

    def _delete_task_log_files(self, task: dict[str, object]) -> int:
        paths: set[Path] = {
            Path(str(entry["path"]))
            for entry in self._list_task_log_entries(task)
            if isinstance(entry.get("path"), str)
        }
        log_path = task.get("log_path")
        if isinstance(log_path, str) and log_path:
            paths.add(Path(log_path))

        deleted_count = 0
        for path in paths:
            try:
                path.unlink()
                deleted_count += 1
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("Failed to delete log file for task %s: %s", task["id"], path)
        return deleted_count

    def _is_task_ready_for_launch(self, task: dict[str, object]) -> bool:
        if not self.database.are_dependencies_satisfied(int(task["id"])):
            return False
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

    def _lease_expires_at(self, ttl_seconds: float | None) -> str | None:
        if ttl_seconds is None:
            return None
        try:
            seconds = float(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("lease TTL 必须是秒数") from exc
        if seconds <= 0 or not seconds < float("inf"):
            raise ValueError("lease TTL 必须大于 0")
        return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()

    def _active_agent_gpu_leases(self) -> list[dict[str, object]]:
        return self.database.list_agent_gpu_leases(include_inactive=False)

    def _leased_gpu_ids(
        self,
        leases: list[dict[str, object]] | None = None,
    ) -> set[int]:
        active_leases = leases if leases is not None else self._active_agent_gpu_leases()
        leased: set[int] = set()
        for lease in active_leases:
            gpu_ids = lease.get("gpu_ids")
            if not isinstance(gpu_ids, list):
                continue
            for gpu_id in gpu_ids:
                try:
                    leased.add(int(gpu_id))
                except (TypeError, ValueError):
                    continue
        return leased

    def _agent_leases_by_gpu(
        self,
        leases: list[dict[str, object]],
    ) -> dict[int, list[dict[str, object]]]:
        by_gpu: dict[int, list[dict[str, object]]] = {}
        for lease in leases:
            gpu_ids = lease.get("gpu_ids")
            if not isinstance(gpu_ids, list):
                continue
            for gpu_id in gpu_ids:
                try:
                    by_gpu.setdefault(int(gpu_id), []).append(lease)
                except (TypeError, ValueError):
                    continue
        return by_gpu

    def _effective_allowed_gpu_set(
        self,
        *,
        known_gpu_ids: set[int],
        active_leases: list[dict[str, object]] | None = None,
    ) -> set[int] | None:
        allowed_gpu_ids = self.database.get_allowed_gpu_ids()
        leased_gpu_ids = self._leased_gpu_ids(active_leases) & known_gpu_ids
        if allowed_gpu_ids is None:
            if not leased_gpu_ids:
                return None
            return set(known_gpu_ids) - leased_gpu_ids
        enabled_gpu_ids = {gpu_id for gpu_id in allowed_gpu_ids if gpu_id in known_gpu_ids}
        return enabled_gpu_ids - leased_gpu_ids

    def _effective_allowed_gpu_ids(
        self,
        *,
        known_gpu_ids: set[int],
        active_leases: list[dict[str, object]] | None = None,
    ) -> list[int] | None:
        allowed_gpu_set = self._effective_allowed_gpu_set(
            known_gpu_ids=known_gpu_ids,
            active_leases=active_leases,
        )
        if allowed_gpu_set is None:
            return None
        return sorted(allowed_gpu_set)

    async def _normalize_gpu_id(self, gpu_id: int) -> int:
        value = int(gpu_id)
        if value < 0:
            raise ValueError("GPU 必须是非负整数")
        known_gpu_ids = await self._known_gpu_ids()
        if value not in known_gpu_ids:
            raise ValueError(f"GPU 不存在: {value}")
        return value

    async def _normalize_requested_gpu(self, requested_gpu: int | None) -> int | None:
        if requested_gpu is None:
            return None
        return await self._normalize_gpu_id(requested_gpu)

    def _normalize_gpu_memory_budget_mb(self, budget_mb: int | None) -> int | None:
        if budget_mb is None:
            return None
        value = int(budget_mb)
        if value <= 0:
            raise ValueError("显存预算必须是正整数 MB")
        return value

    def _normalize_gpu_schedule_action(self, action: str) -> str:
        if action not in {"enable", "disable"}:
            raise ValueError("GPU 定时动作必须是 enable 或 disable")
        return action

    def _normalize_gpu_schedule_time(self, run_at: str) -> datetime:
        try:
            scheduled = datetime.fromisoformat(run_at)
        except ValueError as exc:
            raise ValueError("GPU 定时时间格式无效") from exc
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=UTC)
        scheduled = scheduled.astimezone(UTC)
        if scheduled <= datetime.now(UTC):
            raise ValueError("GPU 定时时间必须晚于当前时间")
        return scheduled

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

    async def _normalize_gpu_id_set(self, gpu_ids: list[int]) -> set[int]:
        normalized = await self._normalize_allowed_gpu_ids(gpu_ids)
        return set(normalized or [])

    async def _known_gpu_ids(self) -> set[int]:
        gpus = await asyncio.to_thread(self._gpu_provider)
        return {gpu.index for gpu in gpus}

    def _load_persisted_scheduler_settings(self) -> None:
        persisted = self.database.get_scheduler_settings()
        source = persisted or {}
        try:
            settings = self._normalize_scheduler_settings(
                poll_interval_seconds=source.get(
                    "poll_interval_seconds",
                    self.config.poll_interval_seconds,
                ),
                gpu_idle_required_checks=source.get(
                    "gpu_idle_required_checks",
                    self.config.gpu_idle_required_checks,
                ),
                auto_restore_idle_gpu_seconds=source.get(
                    "auto_restore_idle_gpu_seconds",
                    self.config.auto_restore_idle_gpu_seconds,
                ),
                auto_retry_enabled=None,
                auto_retry_max_retries=source.get(
                    "auto_retry_max_retries",
                    self.config.auto_retry_max_retries,
                ),
                auto_retry_delay_seconds=source.get(
                    "auto_retry_delay_seconds",
                    self.config.auto_retry_delay_seconds,
                ),
                external_kill_gpu_cooldown_seconds=source.get(
                    "external_kill_gpu_cooldown_seconds",
                    self.config.external_kill_gpu_cooldown_seconds,
                ),
            )
        except ValueError:
            LOGGER.warning("Ignoring invalid persisted scheduler settings: %s", persisted)
            settings = self._normalize_scheduler_settings(
                poll_interval_seconds=self.config.poll_interval_seconds,
                gpu_idle_required_checks=self.config.gpu_idle_required_checks,
                auto_restore_idle_gpu_seconds=(
                    self.config.auto_restore_idle_gpu_seconds
                ),
                auto_retry_enabled=None,
                auto_retry_max_retries=self.config.auto_retry_max_retries,
                auto_retry_delay_seconds=self.config.auto_retry_delay_seconds,
                external_kill_gpu_cooldown_seconds=(
                    self.config.external_kill_gpu_cooldown_seconds
                ),
            )
        self._apply_scheduler_settings(settings)

    def _normalize_scheduler_settings(
        self,
        *,
        poll_interval_seconds: object,
        gpu_idle_required_checks: object,
        auto_restore_idle_gpu_seconds: object,
        auto_retry_enabled: object | None,
        auto_retry_max_retries: object,
        auto_retry_delay_seconds: object,
        external_kill_gpu_cooldown_seconds: object,
    ) -> dict[str, object]:
        try:
            interval = float(poll_interval_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("检测间隔必须是数字") from exc
        if interval <= 0 or not interval < float("inf"):
            raise ValueError("检测间隔必须大于 0")
        try:
            required_checks = int(gpu_idle_required_checks)
        except (TypeError, ValueError) as exc:
            raise ValueError("连续检测次数必须是整数") from exc
        if required_checks < 1:
            raise ValueError("连续检测次数必须大于等于 1")
        auto_restore_seconds = self._normalize_auto_restore_idle_gpu_seconds(
            auto_restore_idle_gpu_seconds
        )
        retry_max_retries = self._normalize_auto_retry_max_retries(
            auto_retry_max_retries
        )
        if auto_retry_enabled is False:
            retry_max_retries = 0
        elif auto_retry_enabled is True and retry_max_retries < 1:
            retry_max_retries = 1
        retry_delay_seconds = self._normalize_auto_retry_delay_seconds(
            auto_retry_delay_seconds
        )
        external_kill_cooldown_seconds = (
            self._normalize_external_kill_gpu_cooldown_seconds(
                external_kill_gpu_cooldown_seconds
            )
        )
        return {
            "poll_interval_seconds": interval,
            "gpu_idle_required_checks": required_checks,
            "auto_restore_idle_gpu_seconds": auto_restore_seconds,
            "auto_retry_max_retries": retry_max_retries,
            "auto_retry_delay_seconds": retry_delay_seconds,
            "external_kill_gpu_cooldown_seconds": external_kill_cooldown_seconds,
        }

    def _normalize_auto_restore_idle_gpu_seconds(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("GPU 自动恢复等待时间必须是数字") from exc
        if seconds < 0 or not seconds < float("inf"):
            raise ValueError("GPU 自动恢复等待时间必须大于等于 0")
        if seconds == 0:
            return None
        return seconds

    def _format_auto_restore_idle_gpu_seconds(self, value: object) -> str:
        if value is None:
            return "关闭"
        seconds = float(value)
        return f"{seconds:g}秒"

    def _normalize_auto_retry_max_retries(self, value: object) -> int:
        try:
            max_retries = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("自动重试次数必须是整数") from exc
        if max_retries < 0:
            raise ValueError("自动重试次数必须大于等于 0")
        return max_retries

    def _normalize_auto_retry_delay_seconds(self, value: object) -> int:
        try:
            delay_seconds = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("自动重试延迟必须是整数秒") from exc
        if delay_seconds < 0:
            raise ValueError("自动重试延迟必须大于等于 0")
        return delay_seconds

    def _normalize_external_kill_gpu_cooldown_seconds(self, value: object) -> float:
        try:
            seconds = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("外部 kill 后 GPU 冷却时间必须是数字") from exc
        if seconds < 0 or not seconds < float("inf"):
            raise ValueError("外部 kill 后 GPU 冷却时间必须大于等于 0")
        return seconds

    def _format_auto_retry_settings(self, settings: dict[str, object]) -> str:
        max_retries = int(settings["auto_retry_max_retries"])
        if max_retries <= 0:
            return "关闭"
        delay_seconds = int(settings["auto_retry_delay_seconds"])
        return f"开启，额外 {max_retries} 次，延迟 {delay_seconds} 秒"

    def _format_external_kill_gpu_cooldown_seconds(self, value: object) -> str:
        seconds = float(value)
        if seconds <= 0:
            return "关闭"
        return f"{seconds:g}秒"

    def _apply_scheduler_settings(self, settings: dict[str, object]) -> None:
        self.config.poll_interval_seconds = float(settings["poll_interval_seconds"])
        self.config.gpu_idle_required_checks = int(settings["gpu_idle_required_checks"])
        auto_restore_seconds = settings.get("auto_restore_idle_gpu_seconds")
        self.config.auto_restore_idle_gpu_seconds = (
            None if auto_restore_seconds is None else float(auto_restore_seconds)
        )
        self.config.auto_retry_max_retries = int(settings["auto_retry_max_retries"])
        self.config.auto_retry_delay_seconds = int(settings["auto_retry_delay_seconds"])
        self.config.external_kill_gpu_cooldown_seconds = float(
            settings["external_kill_gpu_cooldown_seconds"]
        )

    def _scheduler_settings_payload(self) -> dict[str, object]:
        interval = float(self.config.poll_interval_seconds)
        required_checks = int(self.config.gpu_idle_required_checks)
        auto_restore_seconds = self.config.auto_restore_idle_gpu_seconds
        auto_retry_max_retries = int(self.config.auto_retry_max_retries)
        auto_retry_delay_seconds = int(self.config.auto_retry_delay_seconds)
        external_kill_gpu_cooldown_seconds = float(
            self.config.external_kill_gpu_cooldown_seconds
        )
        return {
            "poll_interval_seconds": interval,
            "gpu_idle_required_checks": required_checks,
            "effective_wait_seconds": interval * required_checks,
            "auto_restore_idle_gpu_seconds": auto_restore_seconds,
            "auto_restore_idle_gpu_enabled": (
                auto_restore_seconds is not None and auto_restore_seconds > 0
            ),
            "auto_retry_enabled": auto_retry_max_retries > 0,
            "auto_retry_max_retries": auto_retry_max_retries,
            "auto_retry_delay_seconds": auto_retry_delay_seconds,
            "external_kill_gpu_cooldown_seconds": external_kill_gpu_cooldown_seconds,
        }

    def _reset_gpu_ready_counts(self) -> None:
        self._gpu_ready_counts.clear()
        self._recently_released_gpu_ids.clear()
        self._disabled_gpu_idle_since.clear()

    def _clear_gpu_ready_counts(self, gpu_id: int) -> None:
        stale_keys = [
            key for key in self._gpu_ready_counts
            if key[0] == gpu_id
        ]
        for key in stale_keys:
            self._gpu_ready_counts.pop(key, None)
        self._recently_released_gpu_ids.discard(gpu_id)

    def _mark_gpu_recently_released(self, gpu_id: int) -> None:
        self._recently_released_gpu_ids.add(gpu_id)

    def _start_external_kill_gpu_cooldown(
        self,
        *,
        gpu_id: int,
        task_id: int,
        exit_code: int,
    ) -> dict[str, object]:
        cooldown_seconds = float(self.config.external_kill_gpu_cooldown_seconds)
        if cooldown_seconds <= 0:
            return {}
        now = datetime.now(UTC)
        self._external_kill_gpu_cooldown_started_at[gpu_id] = now
        cooldown_until = now + timedelta(seconds=cooldown_seconds)
        self._clear_gpu_ready_counts(gpu_id)
        self._last_gpu_payload = []
        return {
            "cooldown_gpu_id": gpu_id,
            "cooldown_seconds": cooldown_seconds,
            "cooldown_started_at": now.isoformat(),
            "cooldown_until": cooldown_until.isoformat(),
            "cooldown_reason": "external_signal",
            "cooldown_task_id": task_id,
            "cooldown_exit_code": exit_code,
        }

    def _gpu_external_kill_cooldown_until(
        self,
        gpu_id: int,
        *,
        now: datetime | None = None,
    ) -> datetime | None:
        cooldown_seconds = float(self.config.external_kill_gpu_cooldown_seconds)
        if cooldown_seconds <= 0:
            self._external_kill_gpu_cooldown_started_at.pop(gpu_id, None)
            return None
        started_at = self._external_kill_gpu_cooldown_started_at.get(gpu_id)
        if started_at is None:
            return None
        current_time = now or datetime.now(UTC)
        cooldown_until = started_at + timedelta(seconds=cooldown_seconds)
        if cooldown_until <= current_time:
            self._external_kill_gpu_cooldown_started_at.pop(gpu_id, None)
            return None
        return cooldown_until

    def _is_gpu_in_external_kill_cooldown(
        self,
        gpu_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        return self._gpu_external_kill_cooldown_until(gpu_id, now=now) is not None

    def _prune_external_kill_gpu_cooldowns(
        self,
        *,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(UTC)
        for gpu_id in list(self._external_kill_gpu_cooldown_started_at):
            self._gpu_external_kill_cooldown_until(gpu_id, now=current_time)

    async def _record_operation(
        self,
        *,
        level: str,
        action: str,
        title: str,
        source: str = "scheduler",
        entity_type: str | None = None,
        entity_id: int | None = None,
        detail: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        try:
            log = self.database.add_operation_log(
                level=level,
                source=source,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                title=title,
                detail=detail,
                metadata=metadata,
            )
        except Exception:
            LOGGER.warning("Failed to write operation log: %s", action, exc_info=True)
            return None
        await self.events.publish(
            "operation_log_created",
            {"log_id": log["id"], "action": action},
        )
        return log

    def _task_log_metadata(
        self,
        task: dict[str, object],
        *,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "task_id": task.get("id"),
            "task_name": task.get("name"),
            "status": task.get("status"),
            "command": task.get("command"),
            "cwd": task.get("cwd"),
            "env": task.get("env") if isinstance(task.get("env"), dict) else {},
            "notes": task.get("notes"),
            "requested_gpu": task.get("requested_gpu"),
            "assigned_gpu": task.get("assigned_gpu"),
            "gpu_memory_budget_mb": task.get("gpu_memory_budget_mb"),
            "queue_name": task.get("queue_name"),
            "queue_rank": task.get("queue_rank"),
            "profile_id": task.get("profile_id"),
            "profile_name": task.get("profile_name"),
            "shell_setup": task.get("shell_setup"),
            "attempt_count": task.get("attempt_count"),
            "pid": task.get("pid"),
            "exit_code": task.get("exit_code"),
            "log_path": task.get("log_path"),
            "depends_on": self.database.get_dependency_ids(int(task["id"]))
            if task.get("id") is not None
            else [],
        }
        if extra:
            metadata.update(extra)
        return metadata

    def _profile_log_metadata(
        self,
        profile: dict[str, object],
        *,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "profile_id": profile.get("id"),
            "name": profile.get("name"),
            "cwd": profile.get("cwd"),
            "env": profile.get("env") if isinstance(profile.get("env"), dict) else {},
            "shell_setup": profile.get("shell_setup"),
            "notes": profile.get("notes"),
        }
        if extra:
            metadata.update(extra)
        return metadata

    def _wake_scheduler_loop(self) -> None:
        if not self._stop_event.is_set():
            self._wake_event.set()

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
        try:
            compact_progress_log_file(session.log_path)
        except OSError:
            LOGGER.warning("Failed to compact progress log for task %s", session.task_id, exc_info=True)

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
