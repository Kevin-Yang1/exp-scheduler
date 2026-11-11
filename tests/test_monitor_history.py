import asyncio
from datetime import UTC, datetime
import sqlite3
import time

from exp_scheduler_app.monitor_history import DAY, HOUR, MonitorHistory, MonitorService, metric_values


def test_history_survives_restart_and_includes_pending_batch(tmp_path):
    now = 2_000_000_000.0
    path = tmp_path / "monitor.db"
    history = MonitorHistory(path)
    history.append(now - 4, {"cpu": 12, "memory": None})
    assert history.query(now - 20, now, now=now)["points"][0]["values"] == {"cpu": 12}
    history.flush(now=now)
    restored = MonitorHistory(path)
    restored.append(now - 2, {"cpu": 80})
    result = restored.query(now - 20, now, now=now)
    assert [p["values"]["cpu"] for p in result["points"]] == [12, 80]
    assert result["coverage_start"] == (now - 4) * 1000
    assert result["coverage_end"] == (now - 2) * 1000


def test_rollups_are_weighted_preserve_peaks_and_do_not_duplicate_pending(tmp_path):
    now = 2_000_000_000.0
    history = MonitorHistory(tmp_path / "monitor.db")
    timestamp = int((now - 2 * HOUR) // 60) * 60
    history.append(timestamp, {"cpu": 10})
    history.flush(now=now)
    history.append(timestamp + 2, {"cpu": 30})
    history.append(timestamp + 4, {"cpu": 80})
    result = history.query(timestamp - 5, timestamp + 20, now=now)
    point = result["points"][0]
    assert point["values"]["cpu"] == 40
    assert point["minimum"]["cpu"] == 10
    assert point["maximum"]["cpu"] == 80
    history.flush(now=now)
    assert history.query(timestamp - 5, timestamp + 20, now=now)["points"] == result["points"]


def test_retention_bounds_all_three_tiers(tmp_path):
    now = 2_000_000_000.0
    history = MonitorHistory(tmp_path / "monitor.db")
    for age in (0, 2 * HOUR, 2 * DAY, 8 * DAY):
        history.append(now - age, {"cpu": age})
    history.flush(now=now)
    with sqlite3.connect(history.path) as conn:
        assert dict(conn.execute("SELECT resolution, count(*) FROM metrics GROUP BY resolution")) == {2: 1, 10: 2, 60: 3}
    result = history.query(now - 7 * DAY, now, now=now, max_points=50)
    assert len(result["points"]) == 3
    assert result["step_seconds"] >= 60


def test_missing_samples_stay_missing_and_large_queries_are_bounded(tmp_path):
    now = 2_000_000_000.0
    history = MonitorHistory(tmp_path / "monitor.db")
    for second in range(0, 1000, 2):
        if 400 <= second <= 600:
            continue
        history.append(now - 1000 + second, {"cpu": second})
    points = history.query(now - 1000, now, now=now, max_points=50)["points"]
    assert len(points) <= 51
    assert any(b["time"] - a["time"] > 150_000 for a, b in zip(points, points[1:]))


def test_metric_values_handle_partial_failures():
    values = metric_values({"cpu": None, "memory": {"used_bytes": 1024}, "disks": [
        {"device_id": "8:1", "read_bytes_per_second": None, "write_bytes_per_second": 3},
        {"device_id": "8:2", "read_bytes_per_second": 5, "write_bytes_per_second": 7}]}, None)
    assert values["cpu"] is None
    assert values["read"] == 5 and values["write"] == 10
    assert values["read-8:1"] is None


def test_sampler_collects_without_http_clients_and_flushes_on_shutdown(tmp_path):
    class Metrics:
        def snapshot(self):
            return {"sampled_at": datetime.now(UTC).isoformat(), "cpu": {"percent": 42}, "disks": []}

    async def gpu_provider():
        return []

    async def run():
        service = MonitorService(MonitorHistory(tmp_path / "monitor.db"), Metrics(), gpu_provider)
        await service.startup()
        for _ in range(30):
            if service.history.query(time.time() - 60, time.time())["points"]:
                break
            await asyncio.sleep(.01)
        await service.shutdown()
        points = MonitorHistory(service.history.path).query(time.time() - 60, time.time())["points"]
        assert points[0]["values"]["cpu"] == 42

    asyncio.run(run())
