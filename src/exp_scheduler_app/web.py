from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import SchedulerConfig
from .database import Database
from .scheduler import SchedulerService


STATIC_DIR = Path(__file__).resolve().parent / "static"


class CreateTaskRequest(BaseModel):
    name: str | None = None
    command: str = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    notes: str | None = None
    profile_id: int | None = None


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
                profile_id=payload.profile_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"task": task}

    @app.delete("/api/tasks/{task_id}")
    async def delete_task_endpoint(task_id: int) -> dict[str, object]:
        try:
            await scheduler.delete_task(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/tasks/reorder")
    async def reorder_tasks_endpoint(payload: ReorderTasksRequest) -> dict[str, object]:
        try:
            queue = await scheduler.reorder_tasks(payload.task_ids)
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

    @app.get("/api/tasks/{task_id}/log")
    async def get_task_log_endpoint(task_id: int) -> dict[str, object]:
        try:
            return await scheduler.read_task_log(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
