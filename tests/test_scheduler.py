from __future__ import annotations

from pathlib import Path
import shlex
import sys
import time

from fastapi.testclient import TestClient

from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.database import Database
from exp_scheduler_app.gpu import GPUInfo
from exp_scheduler_app.scheduler import SchedulerService
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


def gpu(
    index: int,
    *,
    idle: bool = True,
    has_processes: bool = False,
    memory_total_mb: int = 24564,
    memory_used_mb: int | None = None,
) -> GPUInfo:
    return GPUInfo(
        index=index,
        uuid=f"GPU-{index}",
        name=f"Fake GPU {index}",
        memory_total_mb=memory_total_mb,
        memory_used_mb=memory_used_mb if memory_used_mb is not None else (500 if idle else 5000),
        utilization_gpu=0 if idle else 82,
        has_processes=has_processes,
    )


def make_config(
    tmp_path,
    *,
    poll_interval_seconds: float = 0.1,
    auto_retry_max_retries: int = 0,
    auto_retry_delay_seconds: int = 5,
) -> SchedulerConfig:
    state_dir = tmp_path / "state"
    return SchedulerConfig(
        host="127.0.0.1",
        port=17861,
        poll_interval_seconds=poll_interval_seconds,
        gpu_idle_memory_mb=1000,
        auto_retry_max_retries=auto_retry_max_retries,
        auto_retry_delay_seconds=auto_retry_delay_seconds,
        state_dir=state_dir,
        log_dir=state_dir / "logs",
    )


def build_client(
    tmp_path,
    provider: FakeGPUProvider,
    *,
    poll_interval_seconds: float = 0.1,
    auto_retry_max_retries: int = 0,
    auto_retry_delay_seconds: int = 5,
) -> TestClient:
    app = create_app(
        make_config(
            tmp_path,
            poll_interval_seconds=poll_interval_seconds,
            auto_retry_max_retries=auto_retry_max_retries,
            auto_retry_delay_seconds=auto_retry_delay_seconds,
        ),
        gpu_provider=provider,
    )
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
    is_urgent: bool = False,
    requested_gpu: int | None = None,
    profile_id: int | None = None,
    gpu_memory_budget_mb: int | None = None,
) -> int:
    response = client.post(
        "/api/tasks",
        json={
            "name": name,
            "command": command_text,
            "env": env or {},
            "cwd": cwd,
            "notes": notes,
            "is_urgent": is_urgent,
            "requested_gpu": requested_gpu,
            "gpu_memory_budget_mb": gpu_memory_budget_mb,
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


def test_task_environment_strips_scheduler_virtualenv(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    service = SchedulerService(config=config, database=database, gpu_provider=lambda: [])

    task = {
        "env": {"HF_HOME": "/tmp/hf-cache"},
    }
    original_env = {
        "PATH": "/workspace/.venv/bin:/usr/local/bin:/usr/bin",
        "VIRTUAL_ENV": "/workspace/.venv",
        "VIRTUAL_ENV_PROMPT": "(.venv) ",
        "_OLD_VIRTUAL_PATH": "/usr/local/bin:/usr/bin",
    }

    import os
    import sys
    from unittest.mock import patch

    with patch.dict("os.environ", original_env, clear=True), patch.object(
        sys,
        "executable",
        "/workspace/.venv/bin/python",
    ):
        env = service._build_task_environment(task=task, gpu_id=1, next_attempt=2)

    assert env["HF_HOME"] == "/tmp/hf-cache"
    assert env["CUDA_VISIBLE_DEVICES"] == "1"
    assert env["EXP_SCHEDULER_ATTEMPT"] == "2"
    assert env["EXP_SCHEDULER_MAX_RETRIES"] == "0"
    assert env["COLUMNS"] == "160"
    assert env["LINES"] == "48"
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert "VIRTUAL_ENV" not in env
    assert "VIRTUAL_ENV_PROMPT" not in env
    assert "_OLD_VIRTUAL_PATH" not in env


def test_running_task_receives_default_terminal_size(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    with build_client(tmp_path, provider) as client:
        task_id = create_task(
            client,
            command(
                "import os, sys; "
                "size = os.get_terminal_size(sys.stdout.fileno()); "
                "print(f'term={size.columns}x{size.lines}', flush=True)"
            ),
            name="term-size",
        )

        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )

        log_payload = client.get(f"/api/tasks/{task_id}/log").json()
        assert "term=160x48" in log_payload["content"]


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


def test_memory_budget_allows_scheduling_when_default_idle_threshold_would_wait(tmp_path):
    provider = FakeGPUProvider([
        gpu(0, idle=False, memory_total_mb=24564, memory_used_mb=5000),
    ])
    with build_client(tmp_path, provider) as client:
        task_id = create_task(
            client,
            command("import os; print('budget-gpu=' + os.environ['CUDA_VISIBLE_DEVICES'])"),
            name="budgeted",
            gpu_memory_budget_mb=16 * 1024,
        )

        history_task = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )
        assert history_task["assigned_gpu"] == 0
        assert history_task["gpu_memory_budget_mb"] == 16 * 1024


def test_memory_budget_waits_until_free_memory_exceeds_budget_plus_headroom(tmp_path):
    provider = FakeGPUProvider([
        gpu(0, idle=False, memory_total_mb=24564, memory_used_mb=7000),
    ])
    with build_client(tmp_path, provider) as client:
        task_id = create_task(
            client,
            command("print('budget-ready')"),
            name="budget-wait",
            gpu_memory_budget_mb=16 * 1024,
        )

        time.sleep(0.5)
        queued = client.get("/api/tasks").json()["queued"]
        assert [task["id"] for task in queued] == [task_id]

        provider.set_gpus([gpu(0, idle=False, memory_total_mb=24564, memory_used_mb=5000)])
        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )


def test_requested_gpu_waits_for_specific_device_while_other_tasks_can_run(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True), gpu(1, idle=False, has_processes=True)])
    with build_client(tmp_path, provider) as client:
        pinned_id = create_task(
            client,
            command("import os; print('pinned=' + os.environ['CUDA_VISIBLE_DEVICES'])"),
            name="pinned",
            requested_gpu=1,
        )
        auto_id = create_task(
            client,
            command("import os; print('auto=' + os.environ['CUDA_VISIBLE_DEVICES'])"),
            name="auto",
        )

        auto_history = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == auto_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )
        assert auto_history["assigned_gpu"] == 0
        queued = client.get("/api/tasks").json()["queued"]
        assert [task["id"] for task in queued] == [pinned_id]

        provider.set_gpus([gpu(0, idle=True), gpu(1, idle=True)])

        pinned_history = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == pinned_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )
        assert pinned_history["assigned_gpu"] == 1
        log_payload = client.get(f"/api/tasks/{pinned_id}/log").json()
        assert "pinned=1" in log_payload["content"]


def test_global_allowed_gpu_ids_limit_scheduling_and_apply_live(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True), gpu(1, idle=True)])
    with build_client(tmp_path, provider) as client:
        response = client.put("/api/settings", json={"allowed_gpu_ids": [1]})
        response.raise_for_status()
        assert response.json()["allowed_gpu_ids"] == [1]

        first_id = create_task(
            client,
            command("import os; print('gpu=' + os.environ['CUDA_VISIBLE_DEVICES'])"),
            name="first",
        )
        first_history = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == first_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )
        assert first_history["assigned_gpu"] == 1

        disable_response = client.put("/api/settings", json={"allowed_gpu_ids": []})
        disable_response.raise_for_status()
        assert disable_response.json()["allowed_gpu_ids"] == []

        second_id = create_task(
            client,
            command("import os; print('gpu=' + os.environ['CUDA_VISIBLE_DEVICES'])"),
            name="second",
        )
        time.sleep(0.3)
        queued = client.get("/api/tasks").json()["queued"]
        assert [task["id"] for task in queued] == [second_id]

        enable_response = client.put("/api/settings", json={"allowed_gpu_ids": [0]})
        enable_response.raise_for_status()
        assert enable_response.json()["allowed_gpu_ids"] == [0]

        second_history = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == second_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )
        assert second_history["assigned_gpu"] == 0


def test_task_completion_triggers_immediate_reschedule_without_waiting_for_poll(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    with build_client(tmp_path, provider, poll_interval_seconds=0.1) as client:
        first_id = create_task(
            client,
            command("import time; time.sleep(0.3)"),
            name="first",
        )

        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["running"]
                if task["id"] == first_id
            ),
            timeout=3,
        )
        client.app.state.scheduler.config.poll_interval_seconds = 5

        second_id = create_task(
            client,
            command("print('second')"),
            name="second",
        )

        second_history = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == second_id and task["status"] == "succeeded"
            ),
            timeout=2,
        )
        assert second_history["assigned_gpu"] == 0


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
        assert client.app.state.scheduler._terminal_sessions == {}


def test_preempt_running_task_runs_urgent_queue_first_and_requeues_to_normal_head(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    order_file = tmp_path / "preempt-order.log"
    with build_client(tmp_path, provider) as client:
        first_id = create_task(
            client,
            command(
                "\n".join(
                    [
                        "import os, time",
                        "from pathlib import Path",
                        "attempt = int(os.environ['EXP_SCHEDULER_ATTEMPT'])",
                        f"path = Path({str(order_file)!r})",
                        "with path.open('a', encoding='utf-8') as fh:",
                        "    fh.write(f'first-attempt-{attempt}\\n')",
                        "time.sleep(30 if attempt == 1 else 0.1)",
                    ]
                )
            ),
            name="first",
        )
        second_id = create_task(
            client,
            command(
                "\n".join(
                    [
                        "from pathlib import Path",
                        f"path = Path({str(order_file)!r})",
                        "with path.open('a', encoding='utf-8') as fh:",
                        "    fh.write('second\\n')",
                    ]
                )
            ),
            name="second",
        )

        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["running"]
                if task["id"] == first_id
            ),
            timeout=3,
        )
        wait_for(
            lambda: (
                order_file.exists()
                and "first-attempt-1" in order_file.read_text(encoding="utf-8")
            ),
            timeout=3,
        )

        urgent_id = create_task(
            client,
            command(
                "\n".join(
                    [
                        "from pathlib import Path",
                        f"path = Path({str(order_file)!r})",
                        "with path.open('a', encoding='utf-8') as fh:",
                        "    fh.write('urgent\\n')",
                    ]
                )
            ),
            name="urgent",
            is_urgent=True,
        )

        preempt_response = client.post(f"/api/tasks/{first_id}/preempt")
        preempt_response.raise_for_status()

        wait_for(
            lambda: (
                len(client.get("/api/tasks").json()["history"]) == 3
                and {
                    task["id"]: task["status"]
                    for task in client.get("/api/tasks").json()["history"]
                }.get(first_id)
                == "succeeded"
                and {
                    task["id"]: task["status"]
                    for task in client.get("/api/tasks").json()["history"]
                }.get(second_id)
                == "succeeded"
                and {
                    task["id"]: task["status"]
                    for task in client.get("/api/tasks").json()["history"]
                }.get(urgent_id)
                == "succeeded"
            ),
            timeout=8,
        )
        assert client.app.state.scheduler._terminal_sessions == {}

    assert order_file.read_text(encoding="utf-8").splitlines() == [
        "first-attempt-1",
        "urgent",
        "first-attempt-2",
        "second",
    ]


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


def test_pty_logs_keep_raw_terminal_bytes_but_text_endpoint_is_sanitized(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    with build_client(tmp_path, provider) as client:
        task_id = create_task(
            client,
            command(
                "import sys; "
                "sys.stdout.write('\\x1b[31mRED\\x1b[0m\\rGREEN\\n'); "
                "sys.stdout.flush()"
            ),
            name="ansi-demo",
        )

        history_task = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )

        raw_bytes = Path(history_task["log_path"]).read_bytes()
        assert b"\x1b[31mRED\x1b[0m\rGREEN" in raw_bytes

        log_payload = client.get(f"/api/tasks/{task_id}/log").json()
        assert "\x1b[" not in log_payload["content"]
        assert "RED" in log_payload["content"]
        assert "GREEN" in log_payload["content"]
        assert client.app.state.scheduler._terminal_sessions == {}


def test_text_log_collapses_tqdm_carriage_return_updates(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    with build_client(tmp_path, provider) as client:
        task_id = create_task(
            client,
            command(
                "import sys; "
                "sys.stdout.write('  0%|zero| 0/10 [00:00<?, ?it/s]\\r'); "
                "sys.stdout.write(' 50%|half| 5/10 [00:01<00:01, 5.00it/s]\\r'); "
                "sys.stdout.write('100%|done| 10/10 [00:02<00:00, 5.00it/s]\\n'); "
                "sys.stdout.flush()"
            ),
            name="progress-demo",
        )

        wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )

        log_payload = client.get(f"/api/tasks/{task_id}/log").json()
        program_output = log_payload["content"].split("[exp-scheduler] attempt=1/1", 1)[1]
        assert "100%|done|" in program_output
        assert "0%|zero|" not in program_output
        assert "50%|half|" not in program_output


def test_retryable_oom_failure_is_automatically_retried(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    with build_client(
        tmp_path,
        provider,
        auto_retry_max_retries=1,
        auto_retry_delay_seconds=0,
    ) as client:
        task_id = create_task(
            client,
            command(
                "\n".join(
                    [
                        "import os, sys",
                        "attempt = int(os.environ['EXP_SCHEDULER_ATTEMPT'])",
                        "print(f'attempt={attempt}')",
                        "if attempt == 1:",
                        "    print('CUDA out of memory')",
                        "    raise SystemExit(1)",
                        "print('recovered')",
                    ]
                )
            )
        )

        history_task = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "succeeded"
            ),
            timeout=8,
        )
        assert history_task["attempt_count"] == 2
        assert history_task["log_path"].endswith("attempt_2.log")
        log_payload = client.get(f"/api/tasks/{task_id}/log").json()
        assert "attempt=2" in log_payload["content"]
        assert "recovered" in log_payload["content"]
        assert client.app.state.scheduler._terminal_sessions == {}


def test_non_retryable_failure_stops_without_retry(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    with build_client(
        tmp_path,
        provider,
        auto_retry_max_retries=3,
        auto_retry_delay_seconds=0,
    ) as client:
        task_id = create_task(
            client,
            command(
                "\n".join(
                    [
                        "import os",
                        "print(f\"attempt={os.environ['EXP_SCHEDULER_ATTEMPT']}\")",
                        "print('plain failure')",
                        "raise SystemExit(1)",
                    ]
                )
            )
        )

        history_task = wait_for(
            lambda: next(
                task
                for task in client.get("/api/tasks").json()["history"]
                if task["id"] == task_id and task["status"] == "failed"
            ),
            timeout=8,
        )
        assert history_task["attempt_count"] == 1
        log_payload = client.get(f"/api/tasks/{task_id}/log").json()
        assert "attempt=1" in log_payload["content"]
        assert "plain failure" in log_payload["content"]
        assert client.app.state.scheduler._terminal_sessions == {}


def test_retryable_task_requeues_to_queue_head(tmp_path):
    provider = FakeGPUProvider([gpu(0, idle=True)])
    order_file = tmp_path / "order.log"
    with build_client(
        tmp_path,
        provider,
        auto_retry_max_retries=1,
        auto_retry_delay_seconds=0,
    ) as client:
        first_task_id = create_task(
            client,
            command(
                "\n".join(
                    [
                        "import os",
                        "from pathlib import Path",
                        "attempt = int(os.environ['EXP_SCHEDULER_ATTEMPT'])",
                        f"path = Path({str(order_file)!r})",
                        "with path.open('a', encoding='utf-8') as fh:",
                        "    fh.write(f'first-attempt-{attempt}\\n')",
                        "if attempt == 1:",
                        "    print('CUDA out of memory')",
                        "    raise SystemExit(1)",
                        "print('first recovered')",
                    ]
                )
            ),
            name="first",
        )
        second_task_id = create_task(
            client,
            command(
                "\n".join(
                    [
                        "from pathlib import Path",
                        f"path = Path({str(order_file)!r})",
                        "with path.open('a', encoding='utf-8') as fh:",
                        "    fh.write('second\\n')",
                        "print('second done')",
                    ]
                )
            ),
            name="second",
        )

        wait_for(
            lambda: (
                len(client.get("/api/tasks").json()["history"]) == 2
                and {
                    task["id"]: task["status"]
                    for task in client.get("/api/tasks").json()["history"]
                }.get(first_task_id)
                == "succeeded"
                and {
                    task["id"]: task["status"]
                    for task in client.get("/api/tasks").json()["history"]
                }.get(second_task_id)
                == "succeeded"
            ),
            timeout=8,
        )

    assert order_file.read_text(encoding="utf-8").splitlines() == [
        "first-attempt-1",
        "first-attempt-2",
        "second",
    ]


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
