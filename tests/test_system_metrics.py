from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.system_metrics import SystemMetrics
from exp_scheduler_app.web import create_app
import exp_scheduler_app.system_metrics as metrics_module


@pytest.fixture
def metrics(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "stat").write_text(
        "cpu 100 20 30 800 50 0 0 0 25 0\n"
        "cpu0 50 0 10 400 40 0 0 0 10 0\n"
        "cpu1 50 20 20 400 10 0 0 0 15 0\n"
    )
    (proc / "cpuinfo").write_text("model name : Test CPU\n")
    (proc / "loadavg").write_text("1.25 2.50 3.75 1/100 1234\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 800 kB\nMemFree: 100 kB\nSwapTotal: 200 kB\nSwapFree: 150 kB\n")
    (proc / "uptime").write_text("12345.67 99999.00\n")
    (proc / "self/mountinfo").write_text(
        "20 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "21 1 8:1 /tmp /tmp rw - ext4 /dev/sda1 rw\n"
        "22 1 0:2 / /proc rw - proc proc rw\n"
        "23 1 0:3 / /home rw - nfs server:/data rw\n"
        "24 1 7:1 / /snap/example ro - squashfs /dev/loop1 ro\n"
    )
    (proc / "diskstats").write_text("8 1 sda1 1 0 100 0 1 0 200 0 0 0 0\n")
    monkeypatch.setattr(metrics_module.os, "statvfs", lambda path: SimpleNamespace(f_blocks=1000, f_bfree=300, f_bavail=200, f_frsize=4096))
    clock = [100.0]
    monkeypatch.setattr(metrics_module.time, "monotonic", lambda: clock[0])
    return SystemMetrics(proc_dir=proc), proc, clock


def test_counter_deltas_memory_and_mount_deduplication(metrics):
    collector, proc, clock = metrics
    first = collector.snapshot()
    assert first["cpu"]["percent"] is None
    assert first["disks"][0]["read_bytes_per_second"] is None
    assert len(first["disks"]) == 1  # bind mounts and pseudo/network mounts excluded
    assert first["memory"]["percent"] == 20.0  # available, rather than MemFree
    assert first["memory"]["swap_percent"] == 25.0
    assert first["disks"][0]["percent"] == 77.8  # reserved blocks excluded, as df does
    assert first["uptime_seconds"] == 12345.67

    (proc / "stat").write_text(
        "cpu 120 20 40 860 60 0 0 0 50 0\n"
        "cpu0 60 0 20 420 50 0 0 0 20 0\n"
        "cpu1 60 20 20 440 10 0 0 0 30 0\n"
    )
    (proc / "diskstats").write_text("8 1 sda1 2 0 108 0 2 0 216 0 0 0 0\n")
    clock[0] += 2
    second = collector.snapshot()
    assert second["sample_interval_seconds"] == 2
    assert second["cpu"]["percent"] == 30.0  # guest already included in user
    assert second["cpu"]["cores"] == [{"index": 0, "percent": 40.0}, {"index": 1, "percent": 20.0}]
    assert second["cpu"]["load_average"] == [1.25, 2.5, 3.75]
    assert second["disks"][0]["read_bytes_per_second"] == 2048
    assert second["disks"][0]["write_bytes_per_second"] == 4096


def test_multiple_clients_share_cached_sampling_window(metrics):
    collector, proc, clock = metrics
    first = collector.snapshot()
    (proc / "stat").unlink()
    clock[0] += 1
    assert collector.snapshot() is first
    clock[0] += 1
    second = collector.snapshot()
    assert second["cpu"] is None
    assert "cpu" in second["errors"]
    assert second["memory"]["total_bytes"] == 1000 * 1024
    assert len(second["disks"]) == 1


def test_missing_io_counters_and_counter_reset_are_unavailable(metrics):
    collector, proc, clock = metrics
    collector.snapshot()
    (proc / "diskstats").write_text("8 1 sda1 1 0 1 0 1 0 2 0 0 0 0\n")
    clock[0] += 2
    reset = collector.snapshot()
    assert reset["disks"][0]["read_bytes_per_second"] is None
    (proc / "diskstats").unlink()
    clock[0] += 2
    missing = collector.snapshot()
    assert missing["disks"][0]["total_bytes"] > 0
    assert missing["disks"][0]["read_bytes_per_second"] is None
    assert "disk_io" in missing["errors"]


def test_unreadable_or_malformed_proc_degrades_by_section(metrics):
    collector, proc, _ = metrics
    (proc / "stat").write_text("cpu invalid\n")
    (proc / "meminfo").write_text("MemFree: 100 kB\n")
    (proc / "self/mountinfo").unlink()
    payload = collector.snapshot()
    assert payload["cpu"] is None
    assert payload["memory"] is None
    assert payload["disks"] == []
    assert payload["uptime_seconds"] > 0
    assert set(payload["errors"]) == {"cpu", "memory", "disks"}


def test_mount_paths_are_unescaped_and_inaccessible_mounts_do_not_break_metrics(metrics, monkeypatch):
    collector, proc, _ = metrics
    (proc / "self/mountinfo").write_text(
        "20 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "21 1 8:2 / /data\\040disk rw - ext4 /dev/sdb1 rw\n"
        "22 1 8:3 / /broken rw - ext4 /dev/sdc1 rw\n"
    )
    original_statvfs = metrics_module.os.statvfs
    seen = []

    def statvfs(path):
        seen.append(path)
        if path == "/broken":
            raise PermissionError("unreadable filesystem")
        return original_statvfs(path)

    monkeypatch.setattr(Path, "is_dir", lambda _: True)
    monkeypatch.setattr(metrics_module.os, "statvfs", statvfs)
    result = collector.snapshot()
    assert "/data disk" in seen
    assert len(result["disks"]) == 2
    assert "disk:/broken" in result["errors"]


def test_metrics_api_works_without_gpus_and_does_not_query_gpu(tmp_path):
    def no_gpu_queries():
        raise AssertionError("Host metrics must not query GPUs or run scheduling")

    app = create_app(SchedulerConfig(state_dir=tmp_path, log_dir=tmp_path / "logs"), gpu_provider=no_gpu_queries, autostart=False)
    app.state.scheduler.database.init()
    with TestClient(app) as client:
        response = client.get("/api/system/metrics")
        assert response.status_code == 200
        payload = response.json()
        assert payload["refresh_interval_seconds"] == 2
        assert payload["cpu"]["logical_count"] > 0
        assert payload["memory"]["total_bytes"] > 0
        assert isinstance(payload["disks"], list)
        assert client.get("/api/system/metrics").json()["sampled_at"] == payload["sampled_at"]
