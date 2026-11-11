from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time

from .system_metrics import SystemMetrics


LOGGER = logging.getLogger(__name__)
HOUR = 3600
DAY = 24 * HOUR
RETENTION = 7 * DAY
TIERS = ((2, HOUR), (10, DAY), (60, RETENTION))


def metric_values(host: dict, gpus: list[dict] | None) -> dict[str, float | None]:
    cpu, memory, disks = host.get("cpu") or {}, host.get("memory") or {}, host.get("disks") or []
    values = {"cpu": cpu.get("percent"), "memory": memory.get("used_bytes")}
    for name in ("read", "write"):
        rates = [disk.get(f"{name}_bytes_per_second") for disk in disks]
        known = [value for value in rates if value is not None]
        values[name] = sum(known) if known else None
        for disk in disks:
            values[f"{name}-{disk['device_id']}"] = disk.get(f"{name}_bytes_per_second")
    for gpu in gpus or []:
        index = gpu["index"]
        values[f"gpu-{index}"] = gpu["utilization_gpu"]
        total = gpu["memory_total_mb"]
        values[f"vram-{index}"] = gpu["memory_used_mb"] / total * 100 if total else None
    return values


def _stats(values: dict) -> dict:
    return {key: [float(value), 1, float(value), float(value)] for key, value in values.items()
            if isinstance(value, (int, float)) and math.isfinite(value)}


def _merge(target: dict, source: dict) -> None:
    for key, values in source.items():
        if key not in target:
            target[key] = values.copy()
        else:
            old = target[key]
            target[key] = [old[0] + values[0], old[1] + values[1], min(old[2], values[2]), max(old[3], values[3])]


class MonitorHistory:
    """Bounded multi-resolution history; the pending batch is also queryable.

    Each metric stores sum/count/min/max, so rollups remain weighted and retain
    peaks. SQLite is separate from scheduler state and written once per batch.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._pending: list[tuple[float, dict]] = []
        self._ready = False
        self._last_flush = time.monotonic()

    @contextmanager
    def _connect(self):
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        if not self._ready:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS metrics (resolution INTEGER, bucket INTEGER, first REAL, last REAL, stats TEXT, PRIMARY KEY(resolution, bucket))")
            conn.commit()
            self._ready = True
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def append(self, timestamp: float, values: dict) -> None:
        with self._lock:
            self._pending.append((timestamp, _stats(values)))
            if time.monotonic() - self._last_flush >= 10 or len(self._pending) >= 10:
                self.flush(now=timestamp)

    @staticmethod
    def _buckets(points, resolution):
        result = {}
        for timestamp, stats in points:
            bucket = int(timestamp // resolution) * resolution
            if bucket not in result:
                result[bucket] = [timestamp, timestamp, {}]
            group = result[bucket]
            group[0], group[1] = min(group[0], timestamp), max(group[1], timestamp)
            _merge(group[2], stats)
        return result

    def flush(self, *, now: float | None = None) -> None:
        with self._lock:
            now = time.time() if now is None else now
            with self._connect() as conn:
                for resolution, retention in TIERS:
                    for bucket, (first, last, stats) in self._buckets(self._pending, resolution).items():
                        if last < now - retention:
                            continue
                        old = conn.execute("SELECT first, last, stats FROM metrics WHERE resolution=? AND bucket=?", (resolution, bucket)).fetchone()
                        if old:
                            first, last = min(first, old[0]), max(last, old[1])
                            _merge(stats, json.loads(old[2]))
                        conn.execute("INSERT OR REPLACE INTO metrics VALUES (?, ?, ?, ?, ?)",
                                     (resolution, bucket, first, last, json.dumps(stats, separators=(",", ":"))))
                    conn.execute("DELETE FROM metrics WHERE resolution=? AND last<?", (resolution, now - retention))
            self._pending.clear()
            self._last_flush = time.monotonic()

    def query(self, start: float, end: float, *, max_points: int = 900, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        if not all(math.isfinite(value) for value in (start, end)) or end <= start or end - start > RETENTION:
            raise ValueError("时间范围必须大于 0 且不超过 7 天")
        if start < now - RETENTION - 60 or end > now + 60:
            raise ValueError("只能查询最近 7 天内的监控数据")
        resolution = 2 if start >= now - HOUR else 10 if start >= now - DAY else 60
        step = max(resolution, math.ceil((end - start) / max_points / resolution) * resolution)
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT bucket, first, last, stats FROM metrics WHERE resolution=? AND bucket>=? AND bucket<=? ORDER BY bucket",
                                (resolution, int(start // resolution) * resolution, end)).fetchall()
            buckets = {bucket: [first, last, json.loads(stats)] for bucket, first, last, stats in rows}
            for bucket, group in self._buckets(self._pending, resolution).items():
                if bucket > end or group[1] < start:
                    continue
                if bucket not in buckets:
                    buckets[bucket] = group
                else:
                    old = buckets[bucket]
                    old[0], old[1] = min(old[0], group[0]), max(old[1], group[1])
                    _merge(old[2], group[2])
            coverage = conn.execute("SELECT MIN(first), MAX(last) FROM metrics WHERE resolution=60").fetchone()
            times = [value for value in coverage if value is not None] + [point[0] for point in self._pending]
        groups = {}
        for bucket, (first, last, stats) in sorted(buckets.items()):
            if last < start or first > end:
                continue
            key = int(bucket // step) * step
            group = groups.setdefault(key, [last, {}])
            group[0] = max(group[0], min(last, end))
            _merge(group[1], stats)
        points = [{"time": round(last * 1000),
                   "values": {key: round(value[0] / value[1], 4) for key, value in stats.items()},
                   "minimum": {key: value[2] for key, value in stats.items()},
                   "maximum": {key: value[3] for key, value in stats.items()}}
                  for last, stats in groups.values()]
        return {"points": points, "step_seconds": step, "retention_seconds": RETENTION,
                "coverage_start": round(min(times) * 1000) if times else None,
                "coverage_end": round(max(times) * 1000) if times else None}


class MonitorService:
    def __init__(self, history: MonitorHistory, metrics: SystemMetrics, gpu_provider) -> None:
        self.history, self.metrics, self.gpu_provider = history, metrics, gpu_provider
        self._task: asyncio.Task | None = None
        self._last_timestamp: float | None = None
        self.error: str | None = None

    async def startup(self):
        await asyncio.to_thread(self.history.flush)
        self._task = asyncio.create_task(self._run(), name="monitor-history")

    async def shutdown(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await asyncio.to_thread(self.history.flush)

    async def _run(self):
        while True:
            started = time.monotonic()
            try:
                host = await asyncio.to_thread(self.metrics.snapshot)
                try:
                    gpus = await asyncio.wait_for(self.gpu_provider(), timeout=3)
                except Exception:
                    gpus = None
                timestamp = datetime.fromisoformat(host["sampled_at"]).timestamp()
                if timestamp != self._last_timestamp:
                    await asyncio.to_thread(self.history.append, timestamp, metric_values(host, gpus))
                    self._last_timestamp = timestamp
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                LOGGER.exception("Monitor history sampling failed")
            await asyncio.sleep(max(.1, 2 - (time.monotonic() - started)))
