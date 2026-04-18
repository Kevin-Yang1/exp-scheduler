from __future__ import annotations

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
