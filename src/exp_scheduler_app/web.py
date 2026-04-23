from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import base64
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import SchedulerConfig
from .database import Database
from .scheduler import SchedulerService


STATIC_DIR = Path(__file__).resolve().parent / "static"


def sse_message(event_name: str, payload: dict[str, object]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class CreateTaskRequest(BaseModel):
    name: str | None = None
    command: str = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None
    is_urgent: bool = False
    requested_gpu: int | None = None
    profile_id: int | None = None


class UpdateTaskRequest(CreateTaskRequest):
    pass


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


class ResizeTerminalRequest(BaseModel):
    cols: int = Field(ge=2, le=1000)
    rows: int = Field(ge=1, le=1000)


def create_app(
    config: SchedulerConfig,
    *,
    gpu_provider=None,
    profile_discovery_provider=None,
    autostart: bool = True,
) -> FastAPI:
    database = Database(config.db_path)
    scheduler = SchedulerService(
        config=config,
        database=database,
        gpu_provider=gpu_provider,
        profile_discovery_provider=profile_discovery_provider,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if autostart:
            await scheduler.startup()
        try:
            yield
        finally:
            if autostart:
                await scheduler.shutdown()

    app = FastAPI(title="exp-scheduler", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.scheduler = scheduler

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
    async def list_tasks() -> dict[str, object]:
        return await scheduler.list_tasks()

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
                requested_gpu=payload.requested_gpu,
                profile_id=payload.profile_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                requested_gpu=payload.requested_gpu,
                profile_id=payload.profile_id,
            )
        except ValueError as exc:
            message = str(exc)
            status_code = 409 if "排队中" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
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

    @app.post("/api/queue/pause")
    async def pause_queue_endpoint() -> dict[str, object]:
        paused = await scheduler.set_queue_paused(True)
        return {"queue_paused": paused}

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

    @app.put("/api/settings")
    async def update_settings_endpoint(payload: UpdateSettingsRequest) -> dict[str, object]:
        try:
            return await scheduler.update_settings(
                allowed_gpu_ids=payload.allowed_gpu_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}/log")
    async def get_task_log_endpoint(task_id: int) -> dict[str, object]:
        try:
            return await scheduler.read_task_log(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/tasks/{task_id}/terminal/stream")
    async def get_task_terminal_stream_endpoint(task_id: int) -> StreamingResponse:
        try:
            _, subscriber, snapshot = await scheduler.subscribe_terminal_stream(task_id)
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
                    for pending_task in pending:
                        pending_task.cancel()
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
