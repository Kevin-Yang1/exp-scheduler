from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import Iterable
from pathlib import Path
import asyncio
import base64
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import SchedulerConfig
from .database import Database
from .scheduler import SchedulerService
from .system_terminal import NvitopTerminalService


STATIC_DIR = Path(__file__).resolve().parent / "static"


def sse_message(event_name: str, payload: dict[str, object]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _cancel_pending_tasks(pending: Iterable[asyncio.Task[Any]]) -> None:
    tasks = list(pending)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class CreateTaskRequest(BaseModel):
    name: str | None = None
    command: str = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None
    is_urgent: bool = False
    queue_name: str | None = None
    requested_gpu: int | None = None
    gpu_memory_budget_mb: int | None = Field(default=None, gt=0)
    profile_id: int | None = None
    depends_on: list[int] = Field(default_factory=list)


class UpdateTaskRequest(CreateTaskRequest):
    depends_on: list[int] | None = None


class UpdateTaskMetadataRequest(BaseModel):
    name: str | None = None
    notes: str | None = None


class SetDependenciesRequest(BaseModel):
    depends_on: list[int]


class MoveTaskQueueRequest(BaseModel):
    queue_name: str


class ProfileRequest(BaseModel):
    name: str = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    shell_setup: str | None = None
    notes: str | None = None


class ImportProfileRequest(ProfileRequest):
    pass


class ReorderTasksRequest(BaseModel):
    task_ids: list[int]
    queue_name: str = "normal"


class UpdateSettingsRequest(BaseModel):
    allowed_gpu_ids: list[int] | None = None
    stop_running_gpu_ids: list[int] = Field(default_factory=list)


class UpdateSchedulerSettingsRequest(BaseModel):
    poll_interval_seconds: float | None = None
    gpu_idle_required_checks: int | None = None
    auto_restore_idle_gpu_seconds: float | None = Field(default=None, ge=0)
    auto_retry_enabled: bool | None = None
    auto_retry_max_retries: int | None = Field(default=None, ge=0)
    auto_retry_delay_seconds: int | None = Field(default=None, ge=0)
    external_kill_gpu_cooldown_seconds: float | None = Field(default=None, ge=0)


class PauseQueueRequest(BaseModel):
    stop_running: bool = False


class ScheduleGpuRequest(BaseModel):
    action: str
    run_at: str


class CreateAgentGpuLeaseRequest(BaseModel):
    owner: str = Field(min_length=1)
    gpu_ids: list[int] = Field(default_factory=list)
    ttl_seconds: float | None = Field(default=3600, gt=0)
    stop_running: bool = False
    notes: str | None = None


class ResizeTerminalRequest(BaseModel):
    cols: int = Field(ge=2, le=1000)
    rows: int = Field(ge=1, le=1000)


def create_app(
    config: SchedulerConfig,
    *,
    gpu_provider=None,
    profile_discovery_provider=None,
    autostart: bool = True,
    nvitop_command: str = "nvitop",
) -> FastAPI:
    database = Database(config.db_path)
    scheduler = SchedulerService(
        config=config,
        database=database,
        gpu_provider=gpu_provider,
        profile_discovery_provider=profile_discovery_provider,
    )
    nvitop_terminal = NvitopTerminalService(
        state_dir=config.state_dir / "system-terminals",
        command=nvitop_command,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if autostart:
            await scheduler.startup()
        try:
            yield
        finally:
            await nvitop_terminal.shutdown()
            if autostart:
                await scheduler.shutdown()

    app = FastAPI(title="exp-scheduler", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.scheduler = scheduler
    app.state.nvitop_terminal = nvitop_terminal

    def add_dependency_payload(
        task: dict[str, object],
        *,
        include_details: bool = False,
    ) -> dict[str, object]:
        dep_ids = scheduler.database.get_dependency_ids(int(task["id"]))
        task["depends_on"] = dep_ids
        task["dependency_count"] = len(dep_ids)
        task["has_dependencies"] = bool(dep_ids)
        if include_details:
            task["dependencies"] = scheduler.database.get_dependencies(int(task["id"]))
        return task

    @app.middleware("http")
    async def disable_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/tasks")
    async def list_tasks(
        history_sort: str = Query(default="finished_at"),
        history_limit: int = Query(default=100, ge=1),
        history_offset: int = Query(default=0, ge=0),
        history_status: str | None = Query(default=None),
    ) -> dict[str, object]:
        if history_sort not in {"finished_at", "started_at"}:
            raise HTTPException(status_code=400, detail="历史排序字段无效")
        if history_status is not None and history_status not in {
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise HTTPException(status_code=400, detail="历史状态无效")
        result = await scheduler.list_tasks(
            history_limit=history_limit,
            history_offset=history_offset,
            history_sort=history_sort,
            history_status=history_status,
        )
        for key in ("queued", "urgent_queued", "staged", "running", "history"):
            for task in result.get(key, []):
                add_dependency_payload(task)
        return result

    @app.get("/api/server")
    async def get_server_info() -> dict[str, object]:
        return {
            "server_name": config.server_name,
            "server_ip": config.server_ip,
            "host": config.host,
            "port": config.port,
        }

    @app.get("/api/profiles")
    async def list_profiles() -> dict[str, object]:
        return {"profiles": await scheduler.list_profiles()}

    @app.get("/api/profiles/discovery")
    async def discover_profiles() -> dict[str, object]:
        return await scheduler.discover_profiles()

    @app.post("/api/profiles")
    async def create_profile_endpoint(payload: ProfileRequest) -> dict[str, object]:
        try:
            profile = await scheduler.create_profile(
                name=payload.name,
                cwd=payload.cwd,
                env=payload.env,
                shell_setup=payload.shell_setup,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"profile": profile}

    @app.post("/api/profiles/import")
    async def import_profile_endpoint(payload: ImportProfileRequest) -> dict[str, object]:
        try:
            profile, renamed_from = await scheduler.import_profile(
                name=payload.name,
                cwd=payload.cwd,
                env=payload.env,
                shell_setup=payload.shell_setup,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"profile": profile, "renamed_from": renamed_from}

    @app.put("/api/profiles/{profile_id}")
    async def update_profile_endpoint(
        profile_id: int,
        payload: ProfileRequest,
    ) -> dict[str, object]:
        try:
            profile = await scheduler.update_profile(
                profile_id,
                name=payload.name,
                cwd=payload.cwd,
                env=payload.env,
                shell_setup=payload.shell_setup,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"profile": profile}

    @app.delete("/api/profiles/{profile_id}")
    async def delete_profile_endpoint(profile_id: int) -> dict[str, object]:
        try:
            await scheduler.delete_profile(profile_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/tasks")
    async def create_task_endpoint(payload: CreateTaskRequest) -> dict[str, object]:
        try:
            task = await scheduler.create_task(
                name=payload.name,
                command=payload.command,
                cwd=payload.cwd,
                env=payload.env,
                notes=payload.notes,
                is_urgent=payload.is_urgent,
                queue_name=payload.queue_name,
                requested_gpu=payload.requested_gpu,
                gpu_memory_budget_mb=payload.gpu_memory_budget_mb,
                profile_id=payload.profile_id,
                depends_on_ids=payload.depends_on,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        add_dependency_payload(task, include_details=True)
        return {"task": task}

    @app.put("/api/tasks/{task_id}")
    async def update_task_endpoint(
        task_id: int,
        payload: UpdateTaskRequest,
    ) -> dict[str, object]:
        try:
            task = await scheduler.update_task(
                task_id,
                name=payload.name,
                command=payload.command,
                cwd=payload.cwd,
                env=payload.env,
                notes=payload.notes,
                is_urgent=payload.is_urgent,
                queue_name=payload.queue_name,
                requested_gpu=payload.requested_gpu,
                gpu_memory_budget_mb=payload.gpu_memory_budget_mb,
                profile_id=payload.profile_id,
                depends_on_ids=payload.depends_on,
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 409 if "排队中" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        add_dependency_payload(task, include_details=True)
        return {"task": task}

    @app.patch("/api/tasks/{task_id}/metadata")
    async def update_task_metadata_endpoint(
        task_id: int,
        payload: UpdateTaskMetadataRequest,
    ) -> dict[str, object]:
        raw_fields_set = getattr(payload, "model_fields_set", None)
        if raw_fields_set is None:
            raw_fields_set = getattr(payload, "__fields_set__", set())
        fields_set = set(raw_fields_set)
        try:
            task = await scheduler.update_task_metadata(
                task_id,
                name=payload.name,
                notes=payload.notes,
                update_name="name" in fields_set,
                update_notes="notes" in fields_set,
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if "不存在" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        add_dependency_payload(task, include_details=True)
        return {"task": task}

    @app.patch("/api/tasks/{task_id}/queue")
    async def move_task_queue_endpoint(
        task_id: int,
        payload: MoveTaskQueueRequest,
    ) -> dict[str, object]:
        try:
            task = await scheduler.move_task_to_queue(
                task_id,
                queue_name=payload.queue_name,
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if "不存在" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        add_dependency_payload(task, include_details=True)
        return {"task": task}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task_endpoint(task_id: int) -> dict[str, object]:
        try:
            await scheduler.delete_task(task_id)
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if "不存在" in message else 409
            raise HTTPException(status_code=status_code, detail=message) from exc
        return {"ok": True}

    @app.post("/api/tasks/reorder")
    async def reorder_tasks_endpoint(payload: ReorderTasksRequest) -> dict[str, object]:
        try:
            queue = await scheduler.reorder_tasks(
                payload.task_ids,
                queue_name=payload.queue_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"queued": queue}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task_endpoint(task_id: int) -> dict[str, object]:
        try:
            await scheduler.cancel_task(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/preempt")
    async def preempt_task_endpoint(task_id: int) -> dict[str, object]:
        try:
            await scheduler.preempt_task(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/requeue")
    async def requeue_task_endpoint(task_id: int) -> dict[str, object]:
        try:
            task = await scheduler.requeue_task(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"task": task}

    @app.get("/api/tasks/{task_id}/dependencies")
    async def get_task_dependencies_endpoint(task_id: int) -> dict[str, object]:
        try:
            info = await scheduler.get_task_dependencies_info(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return info

    @app.put("/api/tasks/{task_id}/dependencies")
    async def set_task_dependencies_endpoint(
        task_id: int, payload: SetDependenciesRequest
    ) -> dict[str, object]:
        try:
            await scheduler.set_task_dependencies(task_id, payload.depends_on)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/queue/pause")
    async def pause_queue_endpoint(payload: PauseQueueRequest | None = None) -> dict[str, object]:
        paused = await scheduler.set_queue_paused(True)
        interrupted = 0
        if payload is not None and payload.stop_running:
            interrupted = await scheduler.interrupt_running_tasks_to_queue_head()
        return {"queue_paused": paused, "interrupted": interrupted}

    @app.post("/api/queue/resume")
    async def resume_queue_endpoint() -> dict[str, object]:
        paused = await scheduler.set_queue_paused(False)
        return {"queue_paused": paused}

    @app.get("/api/gpus")
    async def list_gpus_endpoint() -> dict[str, object]:
        return {"gpus": await scheduler.list_gpus()}

    @app.get("/api/settings")
    async def get_settings_endpoint() -> dict[str, object]:
        return await scheduler.get_settings()

    @app.get("/api/agent/gpu-leases")
    async def list_agent_gpu_leases_endpoint(
        include_inactive: bool = Query(default=False),
    ) -> dict[str, object]:
        return {
            "leases": await scheduler.list_agent_gpu_leases(
                include_inactive=include_inactive,
            )
        }

    @app.post("/api/agent/gpu-leases")
    async def create_agent_gpu_lease_endpoint(
        payload: CreateAgentGpuLeaseRequest,
    ) -> dict[str, object]:
        try:
            return await scheduler.create_agent_gpu_lease(
                owner=payload.owner,
                gpu_ids=payload.gpu_ids,
                ttl_seconds=payload.ttl_seconds,
                stop_running=payload.stop_running,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/agent/gpu-leases/{lease_id}")
    async def release_agent_gpu_lease_endpoint(lease_id: str) -> dict[str, object]:
        try:
            return await scheduler.release_agent_gpu_lease(lease_id)
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if "不存在" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc

    @app.put("/api/settings")
    async def update_settings_endpoint(payload: UpdateSettingsRequest) -> dict[str, object]:
        try:
            return await scheduler.update_settings(
                allowed_gpu_ids=payload.allowed_gpu_ids,
                stop_running_gpu_ids=payload.stop_running_gpu_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/scheduler/settings")
    async def get_scheduler_settings_endpoint() -> dict[str, object]:
        return await scheduler.get_scheduler_settings()

    @app.put("/api/scheduler/settings")
    async def update_scheduler_settings_endpoint(
        payload: UpdateSchedulerSettingsRequest,
    ) -> dict[str, object]:
        raw_fields_set = getattr(payload, "model_fields_set", None)
        if raw_fields_set is None:
            raw_fields_set = getattr(payload, "__fields_set__", set())
        fields_set = set(raw_fields_set)
        current_settings = await scheduler.get_scheduler_settings()
        poll_interval_seconds = (
            payload.poll_interval_seconds
            if "poll_interval_seconds" in fields_set
            else current_settings.get("poll_interval_seconds")
        )
        gpu_idle_required_checks = (
            payload.gpu_idle_required_checks
            if "gpu_idle_required_checks" in fields_set
            else current_settings.get("gpu_idle_required_checks")
        )
        auto_restore_idle_gpu_seconds = (
            payload.auto_restore_idle_gpu_seconds
            if "auto_restore_idle_gpu_seconds" in fields_set
            else current_settings.get("auto_restore_idle_gpu_seconds")
        )
        auto_retry_enabled = (
            payload.auto_retry_enabled
            if "auto_retry_enabled" in fields_set
            else None
        )
        auto_retry_max_retries = (
            payload.auto_retry_max_retries
            if "auto_retry_max_retries" in fields_set
            else current_settings.get("auto_retry_max_retries")
        )
        auto_retry_delay_seconds = (
            payload.auto_retry_delay_seconds
            if "auto_retry_delay_seconds" in fields_set
            else current_settings.get("auto_retry_delay_seconds")
        )
        external_kill_gpu_cooldown_seconds = (
            payload.external_kill_gpu_cooldown_seconds
            if "external_kill_gpu_cooldown_seconds" in fields_set
            else current_settings.get("external_kill_gpu_cooldown_seconds")
        )
        try:
            return await scheduler.update_scheduler_settings(
                poll_interval_seconds=poll_interval_seconds,
                gpu_idle_required_checks=gpu_idle_required_checks,
                auto_restore_idle_gpu_seconds=auto_restore_idle_gpu_seconds,
                auto_retry_enabled=auto_retry_enabled,
                auto_retry_max_retries=auto_retry_max_retries,
                auto_retry_delay_seconds=auto_retry_delay_seconds,
                external_kill_gpu_cooldown_seconds=external_kill_gpu_cooldown_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/settings/gpu-schedule/{gpu_id}")
    async def schedule_gpu_endpoint(
        gpu_id: int,
        payload: ScheduleGpuRequest,
    ) -> dict[str, object]:
        try:
            return await scheduler.schedule_gpu_state(
                gpu_id=gpu_id,
                action=payload.action,
                run_at=payload.run_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/settings/gpu-schedule/{gpu_id}")
    async def clear_gpu_schedule_endpoint(gpu_id: int) -> dict[str, object]:
        try:
            return await scheduler.clear_gpu_schedule(gpu_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}/logs")
    async def list_task_logs_endpoint(task_id: int) -> dict[str, object]:
        try:
            return await scheduler.list_task_logs(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}/log")
    async def get_task_log_endpoint(
        task_id: int,
        attempt: int | None = Query(default=None, ge=1),
        full: bool = Query(default=False),
    ) -> dict[str, object]:
        try:
            if full:
                return await scheduler.read_task_log(task_id, attempt=attempt, tail_bytes=None)
            return await scheduler.read_task_log(task_id, attempt=attempt)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/tasks/{task_id}/logs/{attempt}")
    async def delete_task_log_endpoint(task_id: int, attempt: int) -> dict[str, object]:
        try:
            return await scheduler.delete_task_log(task_id, attempt=attempt)
        except ValueError as exc:
            message = str(exc)
            if "运行中" in message:
                status_code = 409
            elif "无效" in message:
                status_code = 400
            elif "不存在" in message:
                status_code = 404
            else:
                status_code = 400
            raise HTTPException(status_code=status_code, detail=message) from exc

    @app.get("/api/tasks/{task_id}/terminal/stream")
    async def get_task_terminal_stream_endpoint(
        task_id: int,
        full: bool = Query(default=False),
    ) -> StreamingResponse:
        try:
            _, subscriber, snapshot = await scheduler.subscribe_terminal_stream(
                task_id,
                full_snapshot=full,
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if "不存在" in message else 409
            raise HTTPException(status_code=status_code, detail=message) from exc

        async def event_stream():
            try:
                yield sse_message(
                    "snapshot",
                    {
                        "task_id": task_id,
                        "data": base64.b64encode(snapshot).decode("ascii"),
                    },
                )
                while True:
                    chunk_task = asyncio.create_task(subscriber.chunk_queue.get())
                    control_task = asyncio.create_task(subscriber.control_queue.get())
                    done, pending = await asyncio.wait(
                        {chunk_task, control_task},
                        timeout=15,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    await _cancel_pending_tasks(pending)
                    if not done:
                        yield sse_message("heartbeat", {"task_id": task_id})
                        continue
                    if control_task in done:
                        event_type, payload = control_task.result()
                        if event_type == "exit":
                            yield sse_message("exit", payload or {"task_id": task_id})
                        break
                    chunk = chunk_task.result()
                    yield sse_message(
                        "chunk",
                        {
                            "task_id": task_id,
                            "data": base64.b64encode(chunk).decode("ascii"),
                        },
                    )
            finally:
                await scheduler.unsubscribe_terminal_stream(task_id, subscriber)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/tasks/{task_id}/terminal/resize")
    async def resize_task_terminal_endpoint(
        task_id: int,
        payload: ResizeTerminalRequest,
    ) -> dict[str, object]:
        try:
            await scheduler.resize_terminal(
                task_id,
                cols=payload.cols,
                rows=payload.rows,
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 404 if "不存在" in message else 409
            raise HTTPException(status_code=status_code, detail=message) from exc
        return {"ok": True}

    @app.get("/api/system/nvitop/terminal/stream")
    async def get_nvitop_terminal_stream_endpoint(
        cols: int | None = Query(default=None, ge=2, le=1000),
        rows: int | None = Query(default=None, ge=1, le=1000),
    ) -> StreamingResponse:
        try:
            subscriber, snapshot = await nvitop_terminal.subscribe(cols=cols, rows=rows)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        async def event_stream():
            try:
                yield sse_message(
                    "snapshot",
                    {
                        "source": "nvitop",
                        "data": base64.b64encode(snapshot).decode("ascii"),
                    },
                )
                while True:
                    chunk_task = asyncio.create_task(subscriber.chunk_queue.get())
                    control_task = asyncio.create_task(subscriber.control_queue.get())
                    done, pending = await asyncio.wait(
                        {chunk_task, control_task},
                        timeout=15,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    await _cancel_pending_tasks(pending)
                    if not done:
                        yield sse_message("heartbeat", {"source": "nvitop"})
                        continue
                    if control_task in done:
                        event_type, payload = control_task.result()
                        if event_type == "exit":
                            yield sse_message("exit", payload or {"source": "nvitop"})
                        break
                    chunk = chunk_task.result()
                    yield sse_message(
                        "chunk",
                        {
                            "source": "nvitop",
                            "data": base64.b64encode(chunk).decode("ascii"),
                        },
                    )
            finally:
                await nvitop_terminal.unsubscribe(subscriber)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/system/nvitop/terminal/resize")
    async def resize_nvitop_terminal_endpoint(payload: ResizeTerminalRequest) -> dict[str, object]:
        try:
            await nvitop_terminal.resize(cols=payload.cols, rows=payload.rows)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/activity/logs")
    async def list_activity_logs_endpoint(
        limit: int = Query(default=200, ge=1, le=1000),
        level: str | None = None,
        source: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        query: str | None = None,
    ) -> dict[str, object]:
        logs = await scheduler.list_operation_logs(
            limit=limit,
            level=level,
            source=source,
            action=action,
            entity_type=entity_type,
            query=query,
        )
        return {"logs": logs}

    @app.delete("/api/activity/logs")
    async def clear_activity_logs_endpoint() -> dict[str, object]:
        count = await scheduler.clear_operation_logs()
        return {"ok": True, "deleted": count}

    @app.get("/api/events")
    async def events_endpoint() -> StreamingResponse:
        queue = await scheduler.events.subscribe()

        async def event_stream():
            try:
                yield "event: ready\ndata: {}\n\n"
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield "event: heartbeat\ndata: {}\n\n"
                        continue
                    yield f"event: update\ndata: {message}\n\n"
            finally:
                await scheduler.events.unsubscribe(queue)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app
