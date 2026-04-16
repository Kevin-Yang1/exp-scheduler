from __future__ import annotations

import shlex
import sys
import time

from fastapi.testclient import TestClient

from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.database import Database
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


def gpu(index: int, *, idle: bool = True, has_processes: bool = False) -> GPUInfo:
    return GPUInfo(
        index=index,
        uuid=f"GPU-{index}",
        name=f"Fake GPU {index}",
        memory_total_mb=24564,
        memory_used_mb=500 if idle else 5000,
        utilization_gpu=0 if idle else 82,
        has_processes=has_processes,
    )


def make_config(tmp_path) -> SchedulerConfig:
    state_dir = tmp_path / "state"
    return SchedulerConfig(
        host="127.0.0.1",
        port=17861,
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        state_dir=state_dir,
        log_dir=state_dir / "logs",
    )


def build_client(tmp_path, provider: FakeGPUProvider) -> TestClient:
    app = create_app(make_config(tmp_path), gpu_provider=provider)
    return TestClient(app)


def command(script: str) -> str:
    python = shlex.quote(sys.executable)
    return f"{python} -c {shlex.quote(script)}"


def wait_for(assertion, *, timeout: float = 6.0, interval: float = 0.05):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            result = assertion()
            if not result:
                raise AssertionError("result not ready")
            return result
        except AssertionError as exc:
            last_error = exc
        except StopIteration as exc:
            last_error = AssertionError(str(exc))
        time.sleep(interval)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition not met")


def create_profile(
    client: TestClient,
    *,
    name: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    shell_setup: str | None = None,
    notes: str | None = None,
) -> int:
    response = client.post(
        "/api/profiles",
        json={
            "name": name,
            "cwd": cwd,
            "env": env or {},
            "shell_setup": shell_setup,
            "notes": notes,
        },
    )
    response.raise_for_status()
    return response.json()["profile"]["id"]


def create_task(
    client: TestClient,
    command_text: str,
    name: str = "task",
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    notes: str | None = None,
    profile_id: int | None = None,
) -> int:
    response = client.post(
        "/api/tasks",
        json={
            "name": name,
            "command": command_text,
            "env": env or {},
            "cwd": cwd,
            "notes": notes,
            "profile_id": profile_id,
        },
    )
    response.raise_for_status()
    return response.json()["task"]["id"]


def test_reorder_queue_persists_to_database(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=False, has_processes=True)])
    with build_client(tmp_path, provider) as client:
        first = create_task(client, command("print('first')"), name="first")
        second = create_task(client, command("print('second')"), name="second")
        third = create_task(client, command("print('third')"), name="third")

        response = client.post(
            "/api/tasks/reorder",
            json={"task_ids": [third, first, second]},
        )
        response.raise_for_status()

        queued = client.get("/api/tasks").json()["queued"]
        assert [item["id"] for item in queued] == [third, first, second]

    database = Database(make_config(tmp_path).db_path)
    assert [item["id"] for item in database.list_queued_tasks()] == [third, first, second]


def test_scheduler_runs_task_when_gpu_becomes_free(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=False, has_processes=True)])
    with build_client(tmp_path, provider) as client:
        task_id = create_task(
            client,
            command(
                "import os, time; print('gpu=' + os.environ['CUDA_VISIBLE_DEVICES']);"
                "time.sleep(0.2)"
            ),
        )
        queued = client.get("/api/tasks").json()["queued"]
        assert [task["id"] for task in queued] == [task_id]

        provider.set_gpus([gpu(0, idle=True)])

        history_task = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )
        assert history_task["assigned_gpu"] == 0
        log_payload = client.get(f"/api/tasks/{task_id}/log").json()
        assert "gpu=0" in log_payload["content"]


def test_scheduler_does_not_schedule_over_external_process(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True, has_processes=True)])
    with build_client(tmp_path, provider) as client:
        task_id = create_task(client, command("import time; time.sleep(0.2)"))
        time.sleep(1.2)
        queued = client.get("/api/tasks").json()["queued"]
        assert [task["id"] for task in queued] == [task_id]

        provider.set_gpus([gpu(0, idle=True, has_processes=False)])
        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )


def test_two_free_gpus_can_run_two_tasks_in_parallel(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True), gpu(1, idle=True)])
    with build_client(tmp_path, provider) as client:
        first_id = create_task(client, command("import time; time.sleep(2)"), name="first")
        second_id = create_task(client, command("import time; time.sleep(2)"), name="second")

        running = wait_for(
            lambda: (
                current
                if len(current := client.get("/api/tasks").json()["running"]) == 2
                else None
            ),
            timeout=4,
        )
        assert len(running) == 2
        assignments = {task["id"]: task["assigned_gpu"] for task in running}
        assert assignments[first_id] in {0, 1}
        assert assignments[second_id] in {0, 1}
        assert assignments[first_id] != assignments[second_id]


def test_cancel_running_task_transitions_to_cancelled(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    with build_client(tmp_path, provider) as client:
        task_id = create_task(client, command("import time; time.sleep(5)"))

        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["running"]
                if task["id"] == task_id
            )
        )
        response = client.post(f"/api/tasks/{task_id}/cancel")
        response.raise_for_status()

        history_task = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id
            ),
            timeout=8,
        )
        assert history_task["status"] == "cancelled"


def test_task_can_use_environment_profile_defaults_and_shell_setup(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with build_client(tmp_path, provider) as client:
        profile_id = create_profile(
            client,
            name="torch-env",
            cwd=str(project_dir),
            env={"FOO": "from-profile"},
            shell_setup="export PROFILE_HOOK=from-shell",
            notes="demo profile",
        )
        task_id = create_task(
            client,
            command(
                "import os; print('cwd=' + os.getcwd()); "
                "print('foo=' + os.environ['FOO']); "
                "print('hook=' + os.environ['PROFILE_HOOK'])"
            ),
            profile_id=profile_id,
            env={"FOO": "from-task"},
        )

        history_task = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )
        assert history_task["profile_name"] == "torch-env"
        assert history_task["cwd"] == str(project_dir)
        assert history_task["env"]["FOO"] == "from-task"
        log_payload = client.get(f"/api/tasks/{task_id}/log").json()
        assert f"cwd={project_dir}" in log_payload["content"]
        assert "foo=from-task" in log_payload["content"]
        assert "hook=from-shell" in log_payload["content"]


def test_startup_marks_stale_running_tasks_interrupted(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    database.init()
    task = database.create_task(
        name="stale",
        command=command("print('stale')"),
        cwd=None,
        env={},
        notes=None,
    )
    database.mark_task_running(
        task_id=task["id"],
        gpu_id=0,
        pid=12345,
        log_path=str(config.log_dir / "task_stale.log"),
    )

    provider = FakeGPUProvider([gpu(0, idle=True)])
    with TestClient(create_app(config, gpu_provider=provider)):
        current = database.get_task(task["id"])
        assert current is not None
        assert current["status"] == "interrupted"
