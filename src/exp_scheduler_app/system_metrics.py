from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import re
import threading
import time


def _percent(used: int, total: int) -> float:
    return round(min(100.0, max(0.0, used / total * 100)), 1) if total > 0 else 0.0


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


class SystemMetrics:
    """Read-only Linux host metrics, shared and rate-limited across clients.

    CPU and block I/O rates use consecutive samples; the first sample has no
    rate. No sampling thread, subprocess, database writes or blocking sleep.
    """

    def __init__(self, *, proc_dir: Path = Path("/proc"), interval: float = 2.0) -> None:
        self.proc_dir = proc_dir
        self.interval = interval
        self._lock = threading.Lock()
        self._sampled_at: float | None = None
        self._cached: dict[str, object] | None = None
        self._cpu_previous: dict[str, tuple[int, int]] = {}
        self._disk_previous: dict[str, tuple[int, int]] = {}
        self._cpu_model: str | None = None

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._sampled_at if self._sampled_at is not None else None
            if self._cached is not None and elapsed is not None and elapsed < self.interval:
                return self._cached

            errors: dict[str, str] = {}
            payload: dict[str, object] = {
                "sampled_at": datetime.now(UTC).isoformat(),
                "sample_interval_seconds": round(elapsed, 3) if elapsed is not None else None,
                "refresh_interval_seconds": self.interval,
                "cpu": None,
                "memory": None,
                "disks": [],
                "uptime_seconds": None,
                "errors": errors,
            }
            for key, reader in (
                ("cpu", self._cpu),
                ("memory", self._memory),
                ("disks", lambda: self._disks(elapsed, errors)),
                ("uptime_seconds", lambda: float((self.proc_dir / "uptime").read_text().split()[0])),
            ):
                try:
                    payload[key] = reader()
                except (OSError, ValueError, IndexError, KeyError) as exc:
                    errors[key] = str(exc)
                    if key == "cpu":
                        self._cpu_previous = {}
                    elif key == "disks":
                        self._disk_previous = {}
            self._sampled_at = now
            self._cached = payload
            return payload

    def _cpu(self) -> dict[str, object]:
        current: dict[str, tuple[int, int]] = {}
        for line in (self.proc_dir / "stat").read_text().splitlines():
            fields = line.split()
            if not fields or not re.fullmatch(r"cpu\d*", fields[0]):
                continue
            # guest/guest_nice are already included in user/nice.
            ticks = [int(value) for value in fields[1:9]]
            if len(ticks) < 4:
                continue
            current[fields[0]] = (sum(ticks), ticks[3] + (ticks[4] if len(ticks) > 4 else 0))
        if "cpu" not in current:
            raise ValueError("CPU counters unavailable")

        def usage(key: str) -> float | None:
            previous = self._cpu_previous.get(key)
            if previous is None:
                return None
            total = current[key][0] - previous[0]
            idle = current[key][1] - previous[1]
            return _percent(total - idle, total) if total > 0 and idle >= 0 else None

        cores = [{"index": int(key[3:]), "percent": usage(key)} for key in current if key != "cpu"]
        cores.sort(key=lambda core: core["index"])
        result = {"percent": usage("cpu"), "logical_count": len(cores), "cores": cores}
        self._cpu_previous = current
        if self._cpu_model is None:
            try:
                for line in (self.proc_dir / "cpuinfo").read_text().splitlines():
                    key, _, value = line.partition(":")
                    if key.strip() in ("model name", "Hardware"):
                        self._cpu_model = value.strip()
                        break
            except OSError:
                pass
        result["model"] = self._cpu_model
        try:
            result["load_average"] = [float(value) for value in (self.proc_dir / "loadavg").read_text().split()[:3]]
        except (OSError, ValueError):
            result["load_average"] = None
        return result

    def _memory(self) -> dict[str, object]:
        values = {}
        for line in (self.proc_dir / "meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            values[key] = int(value.split()[0]) * 1024
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0) + values.get("Buffers", 0) + values.get("Cached", 0))
        available = min(total, max(0, available))
        swap_total = values.get("SwapTotal", 0)
        swap_used = max(0, swap_total - values.get("SwapFree", 0))
        return {
            "total_bytes": total,
            "used_bytes": total - available,
            "available_bytes": available,
            "percent": _percent(total - available, total),
            "swap_total_bytes": swap_total,
            "swap_used_bytes": swap_used,
            "swap_percent": _percent(swap_used, swap_total),
        }

    def _disks(self, elapsed: float | None, errors: dict[str, str]) -> list[dict[str, object]]:
        counters: dict[str, tuple[int, int]] = {}
        try:
            for line in (self.proc_dir / "diskstats").read_text().splitlines():
                fields = line.split()
                if len(fields) >= 14:
                    # Linux diskstats sector counters always use 512-byte units.
                    counters[f"{fields[0]}:{fields[1]}"] = (int(fields[5]) * 512, int(fields[9]) * 512)
        except (OSError, ValueError) as exc:
            errors["disk_io"] = str(exc)

        mounts = []
        for line in (self.proc_dir / "self/mountinfo").read_text().splitlines():
            before, separator, after = line.partition(" - ")
            fields, filesystem = before.split(), after.split()
            if not separator or len(fields) < 6 or len(filesystem) < 2:
                continue
            mountpoint, device = _unescape_mount(fields[4]), _unescape_mount(filesystem[1])
            # Immutable system images (for example Snap mounts) are normally
            # 100% full by design and are not writable storage capacity.
            if mountpoint != "/" and filesystem[0] in ("squashfs", "iso9660", "udf", "erofs"):
                continue
            # Exclude pseudo/network filesystems and file bind mounts. Root is
            # included for containers/WSL even when backed by overlay or 9p.
            if mountpoint != "/" and not device.startswith("/dev/") and filesystem[0] not in ("zfs", "btrfs"):
                continue
            mounts.append((mountpoint, fields[2], device, filesystem[0]))

        disks = []
        seen: set[str] = set()
        for mountpoint, device_id, device, filesystem in sorted(mounts, key=lambda mount: (mount[0] != "/", len(mount[0]))):
            if device_id in seen:
                continue
            try:
                if not Path(mountpoint).is_dir():
                    continue
                stat = os.statvfs(mountpoint)
            except OSError as exc:
                errors[f"disk:{mountpoint}"] = str(exc)
                continue
            total = stat.f_blocks * stat.f_frsize
            if total <= 0:
                continue
            seen.add(device_id)
            used = max(0, total - stat.f_bfree * stat.f_frsize)
            available = max(0, stat.f_bavail * stat.f_frsize)
            previous, current = self._disk_previous.get(device_id), counters.get(device_id)
            read_rate = write_rate = None
            if previous is not None and current is not None and elapsed is not None and elapsed > 0:
                if all(new >= old for new, old in zip(current, previous)):
                    read_rate, write_rate = (round((new - old) / elapsed, 1) for new, old in zip(current, previous))
            disks.append({
                "device": device,
                "device_id": device_id,
                "mountpoint": mountpoint,
                "filesystem": filesystem,
                "total_bytes": total,
                "used_bytes": used,
                "available_bytes": available,
                # Match df: reserved blocks are excluded from user capacity.
                "percent": _percent(used, used + available),
                "read_bytes_per_second": read_rate,
                "write_bytes_per_second": write_rate,
            })
        self._disk_previous = counters
        return disks
