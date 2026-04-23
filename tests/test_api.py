from __future__ import annotations

import base64
import json
from pathlib import Path
import shlex
import sys
import socket
import threading
import time

from fastapi.testclient import TestClient
import httpx
import uvicorn

from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.gpu import GPUInfo
from exp_scheduler_app.web import create_app


class FakeGPUProvider:
    def __init__(self, gpus: list[GPUInfo]) -> None:
        self._gpus = gpus

    def set_gpus(self, gpus: list[GPUInfo]) -> None:
        self._gpus = gpus

    def __call__(self) -> list[GPUInfo]:
        return [
            GPUInfo(
                index=gpu.index,
                uuid=gpu.uuid,
                name=gpu.name,
                memory_total_mb=gpu.memory_total_mb,
                memory_used_mb=gpu.memory_used_mb,
                utilization_gpu=gpu.utilization_gpu,
                has_processes=gpu.has_processes,
            )
            for gpu in self._gpus
        ]


def gpu(index: int, *, idle: bool = True) -> GPUInfo:
    return GPUInfo(
        index=index,
        uuid=f"GPU-{index}",
        name=f"Fake GPU {index}",
        memory_total_mb=24564,
        memory_used_mb=500 if idle else 5000,
        utilization_gpu=0,
        has_processes=not idle,
    )


def make_client(tmp_path, *, discovery_provider=None) -> TestClient:
    config = SchedulerConfig(
        host="127.0.0.1",
        port=17861,
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        state_dir=tmp_path / "state",
        log_dir=(tmp_path / "state" / "logs"),
    )
    provider = FakeGPUProvider([gpu(0, idle=False)])
    app = create_app(
        config,
        gpu_provider=provider,
        profile_discovery_provider=discovery_provider,
    )
    client = TestClient(app)
    client.fake_gpu_provider = provider
    return client


def command(text: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(text)}"


def wait_for(assertion, *, timeout: float = 6.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = assertion()
            if not result:
                raise AssertionError("result not ready")
            return result
        except (AssertionError, StopIteration):
            time.sleep(interval)
    raise AssertionError("condition not met")


def start_server(tmp_path, provider: FakeGPUProvider) -> tuple[uvicorn.Server, threading.Thread, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = SchedulerConfig(
        host="127.0.0.1",
        port=port,
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        state_dir=tmp_path / "state-live",
        log_dir=(tmp_path / "state-live" / "logs"),
    )
    app = create_app(config, gpu_provider=provider)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="warning")
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)

    return server, server_thread, port


def collect_sse_events(stream, *, stop_when, timeout_seconds: float = 8.0):
    events: list[tuple[str, dict[str, object]]] = []
    event_name: str | None = None
    data_line: str | None = None
    deadline = time.time() + timeout_seconds

    for line in stream.iter_lines():
        if time.time() > deadline:
            break
        if not line:
            if event_name and data_line is not None:
                payload = json.loads(data_line)
                events.append((event_name, payload))
                if stop_when(events):
                    return events
            event_name = None
            data_line = None
            continue
        if line.startswith("event: "):
            event_name = line[len("event: "):]
        elif line.startswith("data: "):
            data_line = line[len("data: "):]

    return events


def test_profile_discovery_and_import_endpoint(tmp_path):
    def fake_discovery():
        return {
            "conda_envs": [
                {
                    "id": "conda::/home/ykw/miniconda3/envs/demo",
                    "kind": "conda",
                    "display_name": "demo",
                    "path": "/home/ykw/miniconda3/envs/demo",
                    "python_path": "/home/ykw/miniconda3/envs/demo/bin/python",
                    "suggested_profile": {
                        "name": "conda:demo",
                        "cwd": None,
                        "env": {},
                        "shell_setup": "source ~/miniconda3/etc/profile.d/conda.sh\nconda activate demo",
                        "notes": "Auto imported from conda environment at /home/ykw/miniconda3/envs/demo",
                    },
                }
            ],
            "venvs": [],
            "search_roots": ["/SSD1/ykw"],
            "conda_executable": "/home/ykw/miniconda3/bin/conda",
        }

    with make_client(tmp_path, discovery_provider=fake_discovery) as client:
        discovery = client.get("/api/profiles/discovery")
        discovery.raise_for_status()
        assert discovery.json()["conda_envs"][0]["display_name"] == "demo"

        imported = client.post(
            "/api/profiles/import",
            json={
                "name": "conda:demo",
                "cwd": None,
                "env": {},
                "shell_setup": "source ~/miniconda3/etc/profile.d/conda.sh\nconda activate demo",
                "notes": "imported",
            },
        )
        imported.raise_for_status()
        assert imported.json()["profile"]["name"] == "conda:demo"
        assert imported.json()["renamed_from"] is None

        imported_again = client.post(
            "/api/profiles/import",
            json={
                "name": "conda:demo",
                "cwd": None,
                "env": {},
                "shell_setup": "source ~/miniconda3/etc/profile.d/conda.sh\nconda activate demo",
                "notes": "imported",
            },
        )
        imported_again.raise_for_status()
        assert imported_again.json()["profile"]["name"] == "conda:demo-2"
        assert imported_again.json()["renamed_from"] == "conda:demo"


def test_profile_crud_and_task_validation(tmp_path):
    with make_client(tmp_path) as client:
        create = client.post(
            "/api/profiles",
            json={
                "name": "conda-a",
                "cwd": "/tmp/project-a",
                "env": {"HF_HOME": "/tmp/hf"},
                "shell_setup": "export PROFILE_A=1",
                "notes": "demo",
            },
        )
        create.raise_for_status()
        profile_id = create.json()["profile"]["id"]

        profiles = client.get("/api/profiles")
        profiles.raise_for_status()
        assert any(profile["id"] == profile_id for profile in profiles.json()["profiles"])

        update = client.put(
            f"/api/profiles/{profile_id}",
            json={
                "name": "conda-b",
                "cwd": "/tmp/project-b",
                "env": {"HF_HOME": "/tmp/hf2"},
                "shell_setup": "export PROFILE_B=1",
                "notes": "updated",
            },
        )
        update.raise_for_status()
        assert update.json()["profile"]["name"] == "conda-b"

        invalid_task = client.post(
            "/api/tasks",
            json={
                "name": "bad",
                "command": command("print('bad')"),
                "cwd": None,
                "env": {},
                "notes": None,
                "profile_id": 99999,
            },
        )
        assert invalid_task.status_code == 400

        delete = client.delete(f"/api/profiles/{profile_id}")
        delete.raise_for_status()
        remaining = client.get("/api/profiles").json()["profiles"]
        assert all(profile["id"] != profile_id for profile in remaining)


def test_gpu_settings_endpoint_and_requested_gpu_validation(tmp_path):
    with make_client(tmp_path) as client:
        settings = client.get("/api/settings")
        settings.raise_for_status()
        assert settings.json()["allowed_gpu_ids"] is None

        update = client.put("/api/settings", json={"allowed_gpu_ids": [0]})
        update.raise_for_status()
        assert update.json()["allowed_gpu_ids"] == [0]

        invalid_settings = client.put("/api/settings", json={"allowed_gpu_ids": [7]})
        assert invalid_settings.status_code == 400

        invalid_task = client.post(
            "/api/tasks",
            json={
                "name": "bad-gpu",
                "command": command("print('bad-gpu')"),
                "cwd": None,
                "env": {},
                "notes": None,
                "requested_gpu": 7,
                "profile_id": None,
            },
        )
        assert invalid_task.status_code == 400


def test_server_info_endpoint_uses_config_values(tmp_path):
    config = SchedulerConfig(
        host="127.0.0.1",
        port=17861,
        server_name="lab-gpu-a",
        server_ip="10.10.0.23",
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        state_dir=tmp_path / "state-server",
        log_dir=(tmp_path / "state-server" / "logs"),
    )
    provider = FakeGPUProvider([gpu(0, idle=False)])
    app = create_app(config, gpu_provider=provider)
    with TestClient(app) as client:
        response = client.get("/api/server")
        response.raise_for_status()
        assert response.json() == {
            "server_name": "lab-gpu-a",
            "server_ip": "10.10.0.23",
            "host": "127.0.0.1",
            "port": 17861,
        }


def test_urgent_queue_listing_and_preempt_requires_waiting_urgent_task(tmp_path):
    with make_client(tmp_path / "listing") as client:
        client.fake_gpu_provider.set_gpus([gpu(0, idle=False)])

        normal = client.post(
            "/api/tasks",
            json={
                "name": "normal-job",
                "command": command("print('normal')"),
                "cwd": None,
                "env": {},
                "notes": None,
                "is_urgent": False,
            },
        )
        normal.raise_for_status()

        urgent = client.post(
            "/api/tasks",
            json={
                "name": "urgent-job",
                "command": command("print('urgent')"),
                "cwd": None,
                "env": {},
                "notes": None,
                "is_urgent": True,
            },
        )
        urgent.raise_for_status()

        tasks = client.get("/api/tasks")
        tasks.raise_for_status()
        assert [task["name"] for task in tasks.json()["queued"]] == ["normal-job"]
        assert [task["name"] for task in tasks.json()["urgent_queued"]] == ["urgent-job"]

    with make_client(tmp_path / "preempt") as client:
        client.fake_gpu_provider.set_gpus([gpu(0, idle=True)])
        running = client.post(
            "/api/tasks",
            json={
                "name": "running-job",
                "command": command("import time; time.sleep(5)"),
                "cwd": None,
                "env": {},
                "notes": None,
            },
        )
        running.raise_for_status()
        running_task = wait_for(
            lambda: next(iter(client.get("/api/tasks").json()["running"])),
            timeout=3,
        )
        preempt = client.post(f"/api/tasks/{running_task['id']}/preempt")
        assert preempt.status_code == 409


def test_update_queued_task_endpoint_supports_in_place_edit(tmp_path):
    with make_client(tmp_path) as client:
        profile = client.post(
            "/api/profiles",
            json={
                "name": "conda-edit",
                "cwd": "/tmp/profile-cwd",
                "env": {"FROM_PROFILE": "1", "A": "profile"},
                "shell_setup": "export PROFILE_READY=1",
                "notes": "profile-notes",
            },
        )
        profile.raise_for_status()
        profile_id = profile.json()["profile"]["id"]

        create = client.post(
            "/api/tasks",
            json={
                "name": "queued-job",
                "command": command("print('before')"),
                "cwd": "/tmp/original",
                "env": {"A": "task", "B": "before"},
                "notes": "before",
                "is_urgent": False,
                "requested_gpu": None,
                "profile_id": None,
            },
        )
        create.raise_for_status()
        task_id = create.json()["task"]["id"]

        update = client.put(
            f"/api/tasks/{task_id}",
            json={
                "name": "edited-job",
                "command": command("print('after')"),
                "cwd": "/tmp/edited",
                "env": {"A": "override", "B": "after"},
                "notes": "after",
                "is_urgent": True,
                "requested_gpu": 0,
                "profile_id": profile_id,
            },
        )
        update.raise_for_status()
        updated_task = update.json()["task"]

        assert updated_task["id"] == task_id
        assert updated_task["name"] == "edited-job"
        assert updated_task["command"] == command("print('after')")
        assert updated_task["cwd"] == "/tmp/edited"
        assert updated_task["env"] == {
            "FROM_PROFILE": "1",
            "A": "override",
            "B": "after",
        }
        assert updated_task["notes"] == "after"
        assert updated_task["queue_name"] == "urgent"
        assert updated_task["requested_gpu"] == 0
        assert updated_task["profile_id"] == profile_id
        assert updated_task["profile_name"] == "conda-edit"
        assert updated_task["shell_setup"] == "export PROFILE_READY=1"

        tasks = client.get("/api/tasks")
        tasks.raise_for_status()
        assert tasks.json()["queued"] == []
        assert [task["id"] for task in tasks.json()["urgent_queued"]] == [task_id]


def test_update_running_task_is_rejected(tmp_path):
    with make_client(tmp_path) as client:
        client.fake_gpu_provider.set_gpus([gpu(0, idle=True)])
        create = client.post(
            "/api/tasks",
            json={
                "name": "running-job",
                "command": command("import time; time.sleep(5)"),
                "cwd": None,
                "env": {},
                "notes": None,
            },
        )
        create.raise_for_status()
        task_id = create.json()["task"]["id"]

        wait_for(
            lambda: next(
                task for task in client.get("/api/tasks").json()["running"] if task["id"] == task_id
            ),
            timeout=3,
        )

        update = client.put(
            f"/api/tasks/{task_id}",
            json={
                "name": "running-job-edited",
                "command": command("print('edited')"),
                "cwd": None,
                "env": {},
                "notes": "should-fail",
                "is_urgent": False,
                "requested_gpu": None,
                "profile_id": None,
            },
        )
        assert update.status_code == 409


def test_pause_resume_delete_and_requeue(tmp_path):
    with make_client(tmp_path) as client:
        create = client.post(
            "/api/tasks",
            json={
                "name": "queued-job",
                "command": command("print('queued')"),
                "cwd": None,
                "env": {},
                "notes": "hello",
            },
        )
        create.raise_for_status()
        task_id = create.json()["task"]["id"]

        pause = client.post("/api/queue/pause")
        pause.raise_for_status()
        assert pause.json()["queue_paused"] is True

        resume = client.post("/api/queue/resume")
        resume.raise_for_status()
        assert resume.json()["queue_paused"] is False

        delete = client.delete(f"/api/tasks/{task_id}")
        delete.raise_for_status()

        client.fake_gpu_provider.set_gpus([gpu(0, idle=True)])
        second = client.post(
            "/api/tasks",
            json={
                "name": "will-fail",
                "command": command("import sys; sys.exit(1)"),
                "cwd": None,
                "env": {},
                "notes": None,
            },
        )
        second.raise_for_status()
        failed_task_id = second.json()["task"]["id"]

        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == failed_task_id and task["status"] == "failed"
            )
        )

        client.fake_gpu_provider.set_gpus([gpu(0, idle=False)])
        requeue = client.post(f"/api/tasks/{failed_task_id}/requeue")
        requeue.raise_for_status()
        new_task = requeue.json()["task"]
        queued_ids = [task["id"] for task in client.get("/api/tasks").json()["queued"]]
        assert new_task["id"] in queued_ids


def test_delete_history_task_removes_record_and_log_file(tmp_path):
    with make_client(tmp_path) as client:
        client.fake_gpu_provider.set_gpus([gpu(0, idle=True)])
        create = client.post(
            "/api/tasks",
            json={
                "name": "history-job",
                "command": command("import sys; print('history-delete'); sys.exit(1)"),
                "cwd": None,
                "env": {},
                "notes": None,
            },
        )
        create.raise_for_status()
        task_id = create.json()["task"]["id"]

        history_task = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "failed"
            ),
            timeout=8,
        )
        log_path = Path(history_task["log_path"])
        assert log_path.exists()

        delete = client.delete(f"/api/tasks/{task_id}")
        delete.raise_for_status()

        history_ids = [task["id"] for task in client.get("/api/tasks").json()["history"]]
        assert task_id not in history_ids
        assert not log_path.exists()

        missing_log = client.get(f"/api/tasks/{task_id}/log")
        assert missing_log.status_code == 404


def test_delete_running_task_is_rejected(tmp_path):
    with make_client(tmp_path) as client:
        client.fake_gpu_provider.set_gpus([gpu(0, idle=True)])
        create = client.post(
            "/api/tasks",
            json={
                "name": "running-delete",
                "command": command("import time; time.sleep(5)"),
                "cwd": None,
                "env": {},
                "notes": None,
            },
        )
        create.raise_for_status()
        task_id = create.json()["task"]["id"]

        wait_for(
            lambda: next(
                task for task in client.get("/api/tasks").json()["running"] if task["id"] == task_id
            ),
            timeout=3,
        )

        delete = client.delete(f"/api/tasks/{task_id}")
        assert delete.status_code == 409


def test_terminal_stream_status_codes_for_missing_and_non_running_tasks(tmp_path):
    with make_client(tmp_path) as client:
        missing = client.get("/api/tasks/999999/terminal/stream")
        assert missing.status_code == 404
        missing_resize = client.post(
            "/api/tasks/999999/terminal/resize",
            json={"cols": 120, "rows": 30},
        )
        assert missing_resize.status_code == 404

        queued = client.post(
            "/api/tasks",
            json={
                "name": "queued-terminal",
                "command": command("print('queued')"),
                "cwd": None,
                "env": {},
                "notes": None,
            },
        )
        queued.raise_for_status()
        queued_id = queued.json()["task"]["id"]

        queued_stream = client.get(f"/api/tasks/{queued_id}/terminal/stream")
        assert queued_stream.status_code == 409
        queued_resize = client.post(
            f"/api/tasks/{queued_id}/terminal/resize",
            json={"cols": 120, "rows": 30},
        )
        assert queued_resize.status_code == 409

        client.fake_gpu_provider.set_gpus([gpu(0, idle=True)])
        history = client.post(
            "/api/tasks",
            json={
                "name": "history-terminal",
                "command": command("print('history')"),
                "cwd": None,
                "env": {},
                "notes": None,
            },
        )
        history.raise_for_status()
        history_id = history.json()["task"]["id"]

        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == history_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )

        history_stream = client.get(f"/api/tasks/{history_id}/terminal/stream")
        assert history_stream.status_code == 409
        history_resize = client.post(
            f"/api/tasks/{history_id}/terminal/resize",
            json={"cols": 120, "rows": 30},
        )
        assert history_resize.status_code == 409


def test_terminal_resize_endpoint_updates_running_pty_size(tmp_path):
    with make_client(tmp_path) as client:
        client.fake_gpu_provider.set_gpus([gpu(0, idle=True)])
        create = client.post(
            "/api/tasks",
            json={
                "name": "terminal-resize",
                "command": command(
                    "import os, sys, time; "
                    "print('ready', flush=True); "
                    "time.sleep(1.2); "
                    "size = os.get_terminal_size(sys.stdout.fileno()); "
                    "print(f'term={size.columns}x{size.lines}', flush=True)"
                ),
                "cwd": None,
                "env": {},
                "notes": None,
            },
        )
        create.raise_for_status()
        task_id = create.json()["task"]["id"]

        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["running"]
                if task["id"] == task_id
            ),
            timeout=4,
        )

        resize = client.post(
            f"/api/tasks/{task_id}/terminal/resize",
            json={"cols": 120, "rows": 30},
        )
        resize.raise_for_status()

        session = client.app.state.scheduler._terminal_sessions[task_id]
        assert session.cols == 120
        assert session.rows == 30

        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )

        log_payload = client.get(f"/api/tasks/{task_id}/log").json()
        assert "term=120x30" in log_payload["content"]


def test_terminal_stream_emits_snapshot_chunk_and_exit_for_running_task(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    server, server_thread, port = start_server(tmp_path, provider)
    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            create = client.post(
                f"{base_url}/api/tasks",
                json={
                    "name": "terminal-live",
                    "command": command(
                        "import sys, time; "
                        "sys.stdout.write('\\x1b[32mboot\\x1b[0m\\n'); sys.stdout.flush(); "
                        "time.sleep(1.2); "
                        "print('tail', flush=True); "
                        "time.sleep(0.1)"
                    ),
                    "cwd": None,
                    "env": {},
                    "notes": None,
                },
            )
            create.raise_for_status()
            task_id = create.json()["task"]["id"]

            wait_for(
                lambda: next(
                    task
                    for task in client.get(f"{base_url}/api/tasks").json()["running"]
                    if task["id"] == task_id
                ),
                timeout=4,
            )

            with client.stream("GET", f"{base_url}/api/tasks/{task_id}/terminal/stream") as stream:
                events = collect_sse_events(
                    stream,
                    stop_when=lambda items: any(name == "exit" for name, _ in items),
                )

        assert events
        assert events[0][0] == "snapshot"
        streamed_bytes = b"".join(
            base64.b64decode(payload["data"])
            for name, payload in events
            if name in {"snapshot", "chunk"}
        )
        assert b"\x1b[32mboot\x1b[0m" in streamed_bytes
        assert b"tail" in streamed_bytes
        assert any(name == "chunk" for name, _ in events)
        exit_payload = next(payload for name, payload in events if name == "exit")
        assert exit_payload["status"] == "succeeded"
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


def test_terminal_stream_reconnect_receives_snapshot_and_continues(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    server, server_thread, port = start_server(tmp_path, provider)
    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            create = client.post(
                f"{base_url}/api/tasks",
                json={
                    "name": "terminal-reconnect",
                    "command": command(
                        "import sys, time; "
                        "print('phase1', flush=True); "
                        "time.sleep(1.4); "
                        "print('phase2', flush=True); "
                        "time.sleep(0.1)"
                    ),
                    "cwd": None,
                    "env": {},
                    "notes": None,
                },
            )
            create.raise_for_status()
            task_id = create.json()["task"]["id"]

            wait_for(
                lambda: next(
                    task
                    for task in client.get(f"{base_url}/api/tasks").json()["running"]
                    if task["id"] == task_id
                ),
                timeout=4,
            )

            with client.stream("GET", f"{base_url}/api/tasks/{task_id}/terminal/stream") as stream:
                first_events = collect_sse_events(
                    stream,
                    stop_when=lambda items: any(name == "snapshot" for name, _ in items),
                    timeout_seconds=3,
                )
            assert first_events and first_events[0][0] == "snapshot"

            time.sleep(0.3)

            with client.stream("GET", f"{base_url}/api/tasks/{task_id}/terminal/stream") as stream:
                second_events = collect_sse_events(
                    stream,
                    stop_when=lambda items: any(name == "exit" for name, _ in items),
                )

        assert second_events
        assert second_events[0][0] == "snapshot"
        reconnected_bytes = b"".join(
            base64.b64decode(payload["data"])
            for name, payload in second_events
            if name in {"snapshot", "chunk"}
        )
        assert b"phase1" in reconnected_bytes
        assert b"phase2" in reconnected_bytes
        assert any(
            name == "chunk" and b"phase2" in base64.b64decode(payload["data"])
            for name, payload in second_events
        )
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)


def test_sse_emits_update_event(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = SchedulerConfig(
        host="127.0.0.1",
        port=port,
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        state_dir=tmp_path / "state-sse",
        log_dir=(tmp_path / "state-sse" / "logs"),
    )
    provider = FakeGPUProvider([gpu(0, idle=False)])
    app = create_app(config, gpu_provider=provider)
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="warning")
    )

    collected: list[str] = []

    def consume_events():
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            with client.stream("GET", f"http://127.0.0.1:{port}/api/events") as stream:
                for line in stream.iter_lines():
                    if line:
                        collected.append(line)
                    if any("task_created" in item for item in collected):
                        return

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)

    worker = threading.Thread(target=consume_events, daemon=True)
    worker.start()
    time.sleep(0.2)
    with httpx.Client(timeout=5.0, trust_env=False) as client:
        response = client.post(
            f"http://127.0.0.1:{port}/api/tasks",
            json={
                "name": "sse-job",
                "command": command("print('hello')"),
                "cwd": None,
                "env": {},
                "notes": None,
            },
        )
        response.raise_for_status()
    worker.join(timeout=3)
    server.should_exit = True
    server_thread.join(timeout=5)
    assert any("task_created" in line for line in collected)
