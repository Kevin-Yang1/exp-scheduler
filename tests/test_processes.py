from datetime import UTC, datetime
import os
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import psutil
import pytest

from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.processes import ProcessService, gpu_processes
from exp_scheduler_app.web import create_app


@pytest.fixture
def child():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield p
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)


def target(child):
    return {"pid": child.pid, "create_time": psutil.Process(child.pid).create_time()}


def wait_status(pid, status):
    for _ in range(100):
        if psutil.Process(pid).status() == status:
            return
        time.sleep(.01)
    raise AssertionError(f"PID {pid} did not become {status}")


def test_gpu_query_includes_graphics_compute_and_multiple_devices(monkeypatch, child):
    xml = f"""<?xml version="1.0"?>
    <nvidia_smi_log>
      <gpu><uuid>GPU-a</uuid><processes>
        <process_info><pid>{child.pid}</pid><type>G</type><used_memory>4 MiB</used_memory></process_info>
        <process_info><pid>123</pid><type>C</type><used_memory>512 MiB</used_memory></process_info>
        <process_info><pid>456</pid><type>C+G</type><used_memory>12.5 MiB</used_memory></process_info>
        <process_info><pid>N/A</pid><used_memory>1 MiB</used_memory></process_info>
      </processes></gpu>
      <gpu><uuid>GPU-b</uuid><processes>
        <process_info><pid>{child.pid}</pid><type>G</type><used_memory>4 MiB</used_memory></process_info>
        <process_info><pid>123</pid><type>C</type><used_memory>0 MiB</used_memory></process_info>
      </processes></gpu>
      <gpu><uuid>GPU-unknown</uuid><processes>
        <process_info><pid>789</pid><type>G</type><used_memory>1 MiB</used_memory></process_info>
      </processes></gpu>
    </nvidia_smi_log>"""
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=xml))
    indices = {"GPU-a": 0, "GPU-b": 2}
    assert gpu_processes(indices) == {
        child.pid: [{"index": 0, "memory_bytes": 4 * 1024 ** 2}, {"index": 2, "memory_bytes": 4 * 1024 ** 2}],
        123: [{"index": 0, "memory_bytes": 512 * 1024 ** 2}, {"index": 2, "memory_bytes": 0}],
        456: [{"index": 0, "memory_bytes": 12.5 * 1024 ** 2}],
    }
    service = ProcessService(lambda: [], interval=0)
    result = service.query(gpu_indices=indices, scope="gpu", gpu=2, search=str(child.pid))
    assert result["total"] == 1
    assert [gpu["index"] for gpu in result["items"][0]["gpus"]] == [0, 2]
    assert service.query(gpu_indices=indices, scope="gpu", gpu=1, search=str(child.pid))["total"] == 0


@pytest.mark.parametrize("memory", ["N/A", "", "-1 MiB", "nan MiB", "unreadable MiB"])
def test_gpu_process_with_unavailable_memory_remains_visible(monkeypatch, memory):
    xml = f"""<nvidia_smi_log><gpu><uuid>GPU-a</uuid><processes>
      <process_info><pid>3637</pid><type>G</type><used_memory>{memory}</used_memory></process_info>
    </processes></gpu></nvidia_smi_log>"""
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=xml))
    assert gpu_processes({"GPU-a": 0}) == {3637: [{"index": 0, "memory_bytes": None}]}


def test_invalid_gpu_process_output_keeps_cpu_processes(monkeypatch, child):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="invalid XML"))
    service = ProcessService(lambda: [])
    result = service.query(gpu_indices={"GPU-a": 0}, search=str(child.pid))
    assert result["total"] == 1
    assert result["items"][0]["gpus"] == []
    assert result["gpu_error"]


def test_gpu_query_without_devices_skips_nvidia_smi(monkeypatch):
    def unexpected_query(*args, **kwargs):
        raise AssertionError("No GPU query expected without devices")
    monkeypatch.setattr(subprocess, "run", unexpected_query)
    assert gpu_processes({}) == {}


def test_query_search_filters_tree_and_nullable_first_sample(child):
    service = ProcessService(lambda: [], interval=0, gpu_process_provider=lambda _: {child.pid: [{"index": 2, "memory_bytes": 1024}]})
    result = service.query(search=str(child.pid), tree=True, scope="mine")
    row = next(row for row in result["items"] if row["pid"] == child.pid)
    assert row["cpu_percent"] is None
    assert row["gpus"][0]["index"] == 2
    assert row["can_signal"] is True
    assert result["total"] == 1
    assert any(row["pid"] == os.getpid() and row["context"] for row in result["items"])
    assert service.query(search=str(child.pid), gpu=1)["total"] == 0
    assert service.query(search=str(child.pid), scope="tasks")["total"] == 0
    time.sleep(.02)
    row = service.query(search=str(child.pid))["items"][0]
    assert row["cpu_percent"] is not None and row["read_bytes_per_second"] is not None


def test_task_association_is_read_only_and_includes_descendants(child):
    service = ProcessService(lambda: [{"pid": os.getpid(), "id": 7, "name": "test", "started_at": datetime.fromtimestamp(psutil.Process().create_time(), UTC).isoformat()}], gpu_process_provider=lambda _: {})
    row = service.query(search=str(child.pid), scope="tasks")["items"][0]
    assert row["task"] == {"id": 7, "name": "test"}
    assert service.send_signal([target(child)], "stop", service.boot_id)[0]["ok"]
    wait_status(child.pid, psutil.STATUS_STOPPED)
    assert service.send_signal([target(child)], "continue", service.boot_id)[0]["ok"]


def test_stop_continue_terminate_and_identity_protection(child):
    service = ProcessService(lambda: [])
    identity = target(child)
    assert not service.send_signal([identity | {"create_time": identity["create_time"] - 100}], "kill", service.boot_id)[0]["ok"]
    assert child.poll() is None
    assert not service.send_signal([identity], "kill", "old-boot")[0]["ok"]
    assert service.send_signal([identity], "stop", service.boot_id)[0]["ok"]
    wait_status(child.pid, psutil.STATUS_STOPPED)
    assert service.send_signal([identity], "continue", service.boot_id)[0]["ok"]
    assert service.send_signal([identity], "terminate", service.boot_id)[0]["ok"]
    assert child.wait(timeout=5) != 0
    assert not service.send_signal([identity], "kill", service.boot_id)[0]["ok"]


def test_batch_reports_each_result_and_protects_service(child):
    service = ProcessService(lambda: [])
    identities = [{"pid": os.getpid(), "create_time": psutil.Process().create_time()}, target(child)]
    results = service.send_signal(identities, "kill", service.boot_id)
    assert [result["ok"] for result in results] == [False, True]
    child.wait(timeout=5)


def test_foreign_user_operation_refused_without_signaling(child):
    service = ProcessService(lambda: [])
    service.uid = os.geteuid() + 1000
    assert not service.send_signal([target(child)], "kill", service.boot_id)[0]["ok"]
    assert child.poll() is None


def test_detail_handles_exit_and_reuse(child):
    service = ProcessService(lambda: [])
    identity = target(child)
    assert "time.sleep" in service.detail(**identity, boot_id=service.boot_id)["command"]
    with pytest.raises(ValueError, match="复用"):
        service.detail(child.pid, identity["create_time"] + 100, service.boot_id)
    child.kill()
    child.wait(timeout=5)
    with pytest.raises(psutil.NoSuchProcess):
        service.detail(**identity, boot_id=service.boot_id)


def test_api_validation_cross_origin_and_audit(tmp_path, child):
    app = create_app(SchedulerConfig(state_dir=tmp_path, log_dir=tmp_path / "logs"), autostart=False, gpu_provider=lambda: [])
    app.state.scheduler.database.init()
    with TestClient(app) as client:
        listing = client.get("/api/system/processes", params={"search": child.pid}).json()
        assert listing["items"][0]["pid"] == child.pid
        payload = {"targets": [target(child)], "action": "stop", "boot_id": listing["boot_id"]}
        assert client.post("/api/system/processes/signal", json=payload, headers={"origin": "https://other.example"}).status_code == 403
        assert child.poll() is None
        assert client.post("/api/system/processes/signal", json=payload | {"action": "shell"}).status_code == 422
        result = client.post("/api/system/processes/signal", json=payload, headers={"origin": "http://testserver"})
        assert result.status_code == 200 and result.json()["results"][0]["ok"]
        wait_status(child.pid, psutil.STATUS_STOPPED)
        logs = app.state.scheduler.database.list_operation_logs()
        assert logs[0]["action"] == "process_stop"
        assert client.post("/api/system/processes/signal", json=payload | {"action": "continue"}).json()["results"][0]["ok"]
        assert client.get("/api/system/processes", params={"sort": "unknown"}).status_code == 400
        assert client.get("/api/system/history", params={"start": time.time() - 8 * 86400}).status_code == 400


def test_gpu_failure_does_not_hide_cpu_processes(tmp_path, child):
    app = create_app(SchedulerConfig(state_dir=tmp_path, log_dir=tmp_path / "logs"), autostart=False)
    app.state.scheduler.database.init()
    async def unavailable():
        raise RuntimeError("GPU unavailable")
    app.state.scheduler.list_gpus = unavailable
    with TestClient(app) as client:
        response = client.get("/api/system/processes", params={"search": child.pid})
        assert response.status_code == 200
        assert response.json()["items"][0]["pid"] == child.pid
        assert response.json()["gpu_error"]
