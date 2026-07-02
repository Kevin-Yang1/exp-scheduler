from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import Iterable
from pathlib import Path
import asyncio
import base64
import json
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .backup import BackupService
from .conda_inventory import CondaInventoryService
from .config import SchedulerConfig
from .database import Database
from .file_browser import FileBrowserService
from .interactive_terminal import MAX_INPUT_BYTES, SNAPSHOT_CHUNK_BYTES, InteractiveTerminalService
from .nodes import NodeRegistryService
from .scheduler import SchedulerService
from .system_terminal import NvitopTerminalService
from .transfer import TransferService


STATIC_DIR = Path(__file__).resolve().parent / "static"


def sse_message(event_name: str, payload: dict[str, object]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _cancel_pending_tasks(pending: Iterable[asyncio.Task[Any]]) -> None:
    tasks = list(pending)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _value_error_to_http(exc: ValueError) -> HTTPException:
    """ValueError → HTTP 状态码映射（沿用既有约定：不存在→404，冲突→409，其余→400）。"""
    message = str(exc)
    if "不存在" in message:
        status_code = 404
    elif any(
        token in message
        for token in ("运行中", "上限", "已退出", "正被", "已处于", "没有可用的传输路由")
    ):
        status_code = 409
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=message)


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
    gpu_memory_reservation_mb: int | None = Field(default=None, gt=0)
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


class CreateSshKeyRequest(BaseModel):
    name: str = Field(min_length=1)
    private_key: str | None = None
    private_key_path: str | None = None
    notes: str | None = None


class UpdateSshKeyRequest(BaseModel):
    name: str | None = None
    notes: str | None = None


class NodeRequest(BaseModel):
    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1)
    auth_method: Literal["key", "password"]
    ssh_key_id: str | None = None
    password: str | None = None
    notes: str | None = None


class ProbeLinksRequest(BaseModel):
    pairs: list[list[str]] | None = None


class TransferPlanRequest(BaseModel):
    src_node_id: str
    src_path: str = Field(min_length=1)
    dst_node_id: str
    dst_path: str = Field(min_length=1)


class CreateTransferRequest(TransferPlanRequest):
    name: str | None = None
    src_contents_only: bool = False
    rsync_args: list[str] = Field(default_factory=list)
    delete_extras: bool = False
    dry_run: bool = False
    route: str = "auto"
    probe_unknown: bool = True


class UpdateTransferSettingsRequest(BaseModel):
    max_concurrent_transfers: int = Field(ge=1, le=8)


class CreateInteractiveTerminalRequest(BaseModel):
    node_id: str = Field(min_length=1)
    cols: int = Field(default=160, ge=2, le=1000)
    rows: int = Field(default=48, ge=1, le=1000)
    name: str | None = Field(default=None, max_length=100)
    startup_command: str | None = Field(default=None, max_length=MAX_INPUT_BYTES)


class TerminalInputRequest(BaseModel):
    data: str = Field(min_length=1, max_length=90000)


class RenameTerminalRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class CreateBackupJobRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    src_node_id: str = Field(min_length=1)
    src_path: str = Field(min_length=1)
    dst_node_id: str = Field(min_length=1)
    dst_path: str = Field(min_length=1)
    schedule_type: str = Field(default="manual")
    schedule_hour: int = Field(default=2, ge=0, le=23)
    schedule_minute: int = Field(default=0, ge=0, le=59)
    schedule_day_of_week: int | None = Field(default=None, ge=0, le=6)
    enabled: bool = True
    delete_extras: bool = False


class UpdateBackupJobRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    schedule_type: str | None = None
    schedule_hour: int | None = Field(default=None, ge=0, le=23)
    schedule_minute: int | None = Field(default=None, ge=0, le=59)
    schedule_day_of_week: int | None = Field(default=None, ge=0, le=6)
    delete_extras: bool | None = None


def create_app(
    config: SchedulerConfig,
    *,
    gpu_provider=None,
    profile_discovery_provider=None,
    autostart: bool = True,
    nvitop_command: str = "nvitop",
    ssh_binary: str = "ssh",
    rsync_binary: str = "rsync",
    sshpass_binary: str = "sshpass",
    ssh_keygen_binary: str = "ssh-keygen",
    conda_inventory_runner=None,
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
    node_registry = NodeRegistryService(
        database=database,
        events=scheduler.events,
        state_dir=config.state_dir,
        server_name=config.server_name,
        ssh_binary=ssh_binary,
        ssh_keygen_binary=ssh_keygen_binary,
        sshpass_binary=sshpass_binary,
    )
    transfer = TransferService(
        config=config,
        database=database,
        events=scheduler.events,
        nodes=node_registry,
        rsync_binary=rsync_binary,
        ssh_binary=ssh_binary,
        sshpass_binary=sshpass_binary,
    )
    interactive_terminals = InteractiveTerminalService(
        state_dir=config.state_dir,
        terminal_log_dir=config.terminal_log_dir,
        events=scheduler.events,
        node_resolver=node_registry.resolve_auth,
        max_sessions=config.max_interactive_terminals,
        history_limit=config.terminal_history_limit,
        max_log_mb=config.terminal_max_log_mb,
        remote_max_log_mb=config.terminal_remote_max_log_mb,
        known_hosts_path=node_registry.known_hosts_path(),
        ssh_binary=ssh_binary,
        sshpass_binary=sshpass_binary,
        database=database,
    )
    conda_inventory = CondaInventoryService(
        nodes=node_registry,
        profile_discovery_provider=profile_discovery_provider,
        runner=conda_inventory_runner,
    )
    file_browser = FileBrowserService(
        node_resolver=node_registry.resolve_auth,
        known_hosts_path=node_registry.known_hosts_path(),
        ssh_binary=ssh_binary,
        sshpass_binary=sshpass_binary,
    )
    backup = BackupService(
        config=config,
        database=database,
        events=scheduler.events,
        node_resolver=node_registry.resolve_auth,
        known_hosts_path=node_registry.known_hosts_path(),
        rsync_binary=rsync_binary,
        ssh_binary=ssh_binary,
        sshpass_binary=sshpass_binary,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if autostart:
            await scheduler.startup()
            await transfer.startup()
            await interactive_terminals.reconcile_on_startup()
            await backup.startup()
        try:
            yield
        finally:
            await interactive_terminals.shutdown()
            await transfer.shutdown()
            await backup.shutdown()
            await nvitop_terminal.shutdown()
            if autostart:
                await scheduler.shutdown()

    app = FastAPI(title="exp-scheduler", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.scheduler = scheduler
    app.state.nvitop_terminal = nvitop_terminal
    app.state.node_registry = node_registry
    app.state.transfer = transfer
    app.state.interactive_terminals = interactive_terminals
    app.state.conda_inventory = conda_inventory

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
                gpu_memory_reservation_mb=payload.gpu_memory_reservation_mb,
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
                gpu_memory_reservation_mb=payload.gpu_memory_reservation_mb,
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

    @app.post("/api/tasks/{task_id}/interrupt")
    async def interrupt_task_endpoint(task_id: int) -> dict[str, object]:
        try:
            await scheduler.interrupt_task_to_staged(task_id)
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

    # ---------- SSH 密钥库 ----------

    @app.get("/api/ssh-keys")
    async def list_ssh_keys_endpoint() -> dict[str, object]:
        return {"keys": await node_registry.list_keys()}

    @app.post("/api/ssh-keys")
    async def create_ssh_key_endpoint(payload: CreateSshKeyRequest) -> dict[str, object]:
        try:
            key = await node_registry.create_key(
                name=payload.name,
                private_key=payload.private_key,
                private_key_path=payload.private_key_path,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"key": key}

    @app.put("/api/ssh-keys/{key_id}")
    async def update_ssh_key_endpoint(
        key_id: str,
        payload: UpdateSshKeyRequest,
    ) -> dict[str, object]:
        try:
            key = await node_registry.update_key(key_id, name=payload.name, notes=payload.notes)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"key": key}

    @app.delete("/api/ssh-keys/{key_id}")
    async def delete_ssh_key_endpoint(key_id: str) -> dict[str, object]:
        try:
            await node_registry.delete_key(key_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"ok": True}

    # ---------- 节点注册表 + 连通性矩阵 ----------

    @app.get("/api/nodes")
    async def list_nodes_endpoint() -> dict[str, object]:
        return {"nodes": await node_registry.list_nodes()}

    @app.post("/api/nodes")
    async def create_node_endpoint(payload: NodeRequest) -> dict[str, object]:
        try:
            node = await node_registry.create_node(
                name=payload.name,
                host=payload.host,
                ssh_port=payload.ssh_port,
                username=payload.username,
                auth_method=payload.auth_method,
                ssh_key_id=payload.ssh_key_id,
                password=payload.password,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"node": node}

    # 注意：/api/nodes/links* 必须声明在 /api/nodes/{node_id} 之前，避免被路径参数吞掉
    @app.get("/api/nodes/links")
    async def list_node_links_endpoint() -> dict[str, object]:
        return await node_registry.links_payload()

    @app.post("/api/nodes/links/probe", status_code=202)
    async def probe_node_links_endpoint(
        payload: ProbeLinksRequest | None = None,
    ) -> dict[str, object]:
        pairs = payload.pairs if payload is not None else None
        try:
            return await node_registry.probe_links(pairs)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc

    @app.put("/api/nodes/{node_id}")
    async def update_node_endpoint(node_id: str, payload: NodeRequest) -> dict[str, object]:
        try:
            node = await node_registry.update_node(
                node_id,
                name=payload.name,
                host=payload.host,
                ssh_port=payload.ssh_port,
                username=payload.username,
                auth_method=payload.auth_method,
                ssh_key_id=payload.ssh_key_id,
                password=payload.password,
                notes=payload.notes,
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"node": node}

    @app.delete("/api/nodes/{node_id}")
    async def delete_node_endpoint(node_id: str) -> dict[str, object]:
        try:
            await node_registry.delete_node(node_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"ok": True}

    @app.post("/api/nodes/{node_id}/test")
    async def test_node_endpoint(node_id: str) -> dict[str, object]:
        try:
            return await node_registry.test_node(node_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc

    # ---------- 目录浏览 ----------

    @app.get("/api/files/browse")
    async def browse_directory_endpoint(
        node_id: str = Query(min_length=1),
        path: str = Query(default="~"),
    ) -> dict[str, object]:
        try:
            return await file_browser.list_directory(node_id, path)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc

    # ---------- 文件传输 ----------
    # 注意：/api/transfers/plan 与 /api/transfers/settings 必须声明在 /api/transfers/{job_id} 之前

    @app.post("/api/transfers/plan")
    async def plan_transfer_routes_endpoint(
        payload: TransferPlanRequest,
        probe: bool = Query(default=False),
    ) -> dict[str, object]:
        try:
            return await transfer.plan_routes(
                payload.src_node_id,
                payload.src_path,
                payload.dst_node_id,
                payload.dst_path,
                probe=probe,
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc

    @app.get("/api/transfers/settings")
    async def get_transfer_settings_endpoint() -> dict[str, object]:
        return await transfer.get_settings()

    @app.put("/api/transfers/settings")
    async def update_transfer_settings_endpoint(
        payload: UpdateTransferSettingsRequest,
    ) -> dict[str, object]:
        try:
            return await transfer.update_settings(
                max_concurrent_transfers=payload.max_concurrent_transfers,
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc

    @app.get("/api/transfers")
    async def list_transfers_endpoint(
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        if status is not None and status not in {
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
        }:
            raise HTTPException(status_code=400, detail="历史状态无效")
        history = await transfer.list_jobs(status=status, limit=limit, offset=offset)
        if status is None:
            history = [job for job in history if job["status"] not in ("pending", "running")]
        return {
            "active": database.list_active_transfer_jobs(),
            "history": history,
        }

    @app.post("/api/transfers")
    async def create_transfer_endpoint(payload: CreateTransferRequest) -> dict[str, object]:
        try:
            job = await transfer.create_job(
                payload.src_node_id,
                payload.src_path,
                payload.dst_node_id,
                payload.dst_path,
                name=payload.name,
                rsync_args=payload.rsync_args,
                delete_extras=payload.delete_extras,
                dry_run=payload.dry_run,
                route=payload.route,
                probe_unknown=payload.probe_unknown,
                src_contents_only=payload.src_contents_only,
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"job": job}

    @app.get("/api/transfers/{job_id}")
    async def get_transfer_endpoint(job_id: str) -> dict[str, object]:
        job = database.get_transfer_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"传输任务不存在: {job_id}")
        return {"job": job}

    @app.post("/api/transfers/{job_id}/cancel")
    async def cancel_transfer_endpoint(job_id: str) -> dict[str, object]:
        try:
            job = await transfer.cancel_job(job_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"job": job}

    @app.delete("/api/transfers/{job_id}")
    async def delete_transfer_endpoint(job_id: str) -> dict[str, object]:
        try:
            await transfer.delete_job(job_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"ok": True}

    @app.get("/api/transfers/{job_id}/log")
    async def get_transfer_log_endpoint(
        job_id: str,
        full: bool = Query(default=False),
    ) -> dict[str, object]:
        try:
            if full:
                return await transfer.read_job_log(job_id, tail_bytes=None)
            return await transfer.read_job_log(job_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc

    # ---------- 多节点交互终端 ----------

    @app.get("/api/terminals")
    async def list_interactive_terminals_endpoint() -> dict[str, object]:
        return {"sessions": await interactive_terminals.list_sessions()}

    @app.post("/api/terminals")
    async def create_interactive_terminal_endpoint(
        payload: CreateInteractiveTerminalRequest,
    ) -> dict[str, object]:
        startup_data = (
            payload.startup_command.encode("utf-8")
            if payload.startup_command
            else None
        )
        if startup_data is not None and len(startup_data) > MAX_INPUT_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"启动命令超过 {MAX_INPUT_BYTES // 1024}KB 上限",
            )
        try:
            session = await interactive_terminals.create_session(
                payload.node_id,
                cols=payload.cols,
                rows=payload.rows,
                name=payload.name,
            )
            if startup_data is not None:
                await interactive_terminals.write_startup_input(
                    str(session["session_id"]),
                    startup_data,
                )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"session": session}

    @app.patch("/api/terminals/{session_id}")
    async def rename_interactive_terminal_endpoint(
        session_id: str,
        payload: RenameTerminalRequest,
    ) -> dict[str, object]:
        try:
            info = await interactive_terminals.rename_session(session_id, payload.name)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"session": info}

    @app.get("/api/terminals/{session_id}/stream")
    async def get_interactive_terminal_stream_endpoint(
        session_id: str,
        cols: int | None = Query(default=None, ge=2, le=1000),
        rows: int | None = Query(default=None, ge=1, le=1000),
    ) -> StreamingResponse:
        try:
            subscriber, snapshot, _info = await interactive_terminals.subscribe(
                session_id,
                cols=cols,
                rows=rows,
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc

        async def event_stream():
            try:
                # 分块 snapshot 协议：snapshot_start → snapshot_chunk* → snapshot_done
                yield sse_message(
                    "snapshot_start",
                    {
                        "session_id": session_id,
                        "total_bytes": len(snapshot),
                        "cols": _info.get("cols") if isinstance(_info, dict) else None,
                        "rows": _info.get("rows") if isinstance(_info, dict) else None,
                    },
                )
                for offset in range(0, len(snapshot), SNAPSHOT_CHUNK_BYTES):
                    chunk = snapshot[offset : offset + SNAPSHOT_CHUNK_BYTES]
                    yield sse_message(
                        "snapshot_chunk",
                        {
                            "session_id": session_id,
                            "data": base64.b64encode(chunk).decode("ascii"),
                        },
                    )
                yield sse_message("snapshot_done", {"session_id": session_id})
                # live 流
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
                        yield sse_message("heartbeat", {"session_id": session_id})
                        continue
                    if control_task in done:
                        event_type, payload = control_task.result()
                        if event_type == "exit":
                            exit_payload = dict(payload or {})
                            exit_payload.setdefault("session_id", session_id)
                            yield sse_message("exit", exit_payload)
                        break
                    chunk = chunk_task.result()
                    yield sse_message(
                        "chunk",
                        {
                            "session_id": session_id,
                            "data": base64.b64encode(chunk).decode("ascii"),
                        },
                    )
            finally:
                await interactive_terminals.unsubscribe(session_id, subscriber)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/terminals/{session_id}/input")
    async def write_interactive_terminal_input_endpoint(
        session_id: str,
        payload: TerminalInputRequest,
    ) -> dict[str, object]:
        try:
            data = base64.b64decode(payload.data, validate=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="输入数据不是有效的 base64") from exc
        if len(data) > MAX_INPUT_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"单次输入超过 {MAX_INPUT_BYTES // 1024}KB 上限",
            )
        try:
            await interactive_terminals.write_input(session_id, data)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"ok": True}

    @app.post("/api/terminals/{session_id}/resize")
    async def resize_interactive_terminal_endpoint(
        session_id: str,
        payload: ResizeTerminalRequest,
    ) -> dict[str, object]:
        try:
            await interactive_terminals.resize(session_id, cols=payload.cols, rows=payload.rows)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"ok": True}

    @app.delete("/api/terminals/{session_id}")
    async def close_interactive_terminal_endpoint(session_id: str) -> dict[str, object]:
        try:
            await interactive_terminals.close_session(session_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"ok": True}

    @app.get("/api/terminals/{session_id}/log")
    async def read_live_terminal_log_endpoint(
        session_id: str,
        tail: int | None = Query(default=None, ge=1, le=10 * 1024 * 1024),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        try:
            data = await interactive_terminals.read_live_log(
                session_id, tail_bytes=tail, offset=offset
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {
            "session_id": session_id,
            "data": base64.b64encode(data).decode("ascii"),
            "size": len(data),
        }

    # ---------- 归档终端日志 ----------

    @app.get("/api/terminals/logs")
    async def list_archived_terminal_logs_endpoint() -> dict[str, object]:
        return {"archives": await interactive_terminals.list_archived()}

    @app.get("/api/terminals/logs/{terminal_id}")
    async def read_archived_terminal_log_endpoint(
        terminal_id: str,
        tail: int | None = Query(default=None, ge=1, le=10 * 1024 * 1024),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        try:
            data = await interactive_terminals.read_archived_log(
                terminal_id, tail_bytes=tail, offset=offset
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {
            "terminal_id": terminal_id,
            "data": base64.b64encode(data).decode("ascii"),
            "size": len(data),
        }

    # ---------- 定时备份 ----------

    @app.get("/api/backups")
    async def list_backup_jobs_endpoint() -> dict[str, object]:
        return {"jobs": await backup.list_jobs()}

    @app.post("/api/backups")
    async def create_backup_job_endpoint(
        payload: CreateBackupJobRequest,
    ) -> dict[str, object]:
        try:
            job = await backup.create_job(
                name=payload.name,
                src_node_id=payload.src_node_id,
                src_path=payload.src_path,
                dst_node_id=payload.dst_node_id,
                dst_path=payload.dst_path,
                schedule_type=payload.schedule_type,
                schedule_hour=payload.schedule_hour,
                schedule_minute=payload.schedule_minute,
                schedule_day_of_week=payload.schedule_day_of_week,
                enabled=payload.enabled,
                delete_extras=payload.delete_extras,
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"job": job}

    @app.get("/api/backups/{job_id}")
    async def get_backup_job_endpoint(job_id: str) -> dict[str, object]:
        try:
            job = await backup.get_job(job_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        if job is None:
            raise HTTPException(status_code=404, detail=f"备份任务不存在: {job_id}")
        return {"job": job}

    @app.patch("/api/backups/{job_id}")
    async def update_backup_job_endpoint(
        job_id: str,
        payload: UpdateBackupJobRequest,
    ) -> dict[str, object]:
        try:
            job = await backup.update_job(
                job_id,
                name=payload.name,
                enabled=payload.enabled,
                schedule_type=payload.schedule_type,
                schedule_hour=payload.schedule_hour,
                schedule_minute=payload.schedule_minute,
                schedule_day_of_week=payload.schedule_day_of_week,
                delete_extras=payload.delete_extras,
            )
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        if job is None:
            raise HTTPException(status_code=404, detail=f"备份任务不存在: {job_id}")
        return {"job": job}

    @app.delete("/api/backups/{job_id}")
    async def delete_backup_job_endpoint(job_id: str) -> dict[str, object]:
        try:
            ok = await backup.delete_job(job_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"ok": ok}

    @app.post("/api/backups/{job_id}/run")
    async def trigger_backup_run_endpoint(job_id: str) -> dict[str, object]:
        try:
            run = await backup.trigger_run(job_id)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc
        return {"run": run}

    @app.get("/api/backups/{job_id}/runs")
    async def list_backup_runs_endpoint(
        job_id: str,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, object]:
        return {"runs": await backup.list_runs(job_id=job_id, limit=limit)}

    @app.get("/api/backups/runs/{run_id}/log")
    async def read_backup_run_log_endpoint(
        run_id: int,
        full: bool = Query(default=False),
    ) -> dict[str, object]:
        try:
            return await backup.read_run_log(run_id, tail_bytes=None if full else 64 * 1024)
        except ValueError as exc:
            raise _value_error_to_http(exc) from exc

    # ---------- conda 环境对比 ----------

    @app.get("/api/conda/inventory")
    async def get_conda_inventory_endpoint() -> dict[str, object]:
        return await conda_inventory.get_inventory(refresh=False)

    @app.post("/api/conda/inventory/refresh")
    async def refresh_conda_inventory_endpoint() -> dict[str, object]:
        return await conda_inventory.get_inventory(refresh=True)

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
