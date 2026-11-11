from __future__ import annotations

from datetime import datetime
import math
import os
from pathlib import Path
import pwd
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

import psutil


SIGNALS = {"terminate": signal.SIGTERM, "kill": signal.SIGKILL,
           "stop": signal.SIGSTOP, "continue": signal.SIGCONT}
SORT_FIELDS = {"pid", "user", "cpu_percent", "memory_percent", "rss_bytes", "elapsed_seconds", "read_bytes_per_second", "write_bytes_per_second", "name"}


def gpu_processes(indices: dict[str, int]) -> dict[int, list[dict]]:
    if not indices:
        return {}
    # The compute-only query omits graphics clients such as Xorg. The complete
    # process list includes compute, graphics and mixed contexts on each GPU.
    result = subprocess.run(["nvidia-smi", "-q", "-x"],
                            capture_output=True, text=True, timeout=3, check=True)
    try:
        root = ET.fromstring(result.stdout)
    except ET.ParseError as exc:
        raise ValueError("NVIDIA GPU 进程数据格式无效") from exc
    processes: dict[int, list[dict]] = {}
    for gpu in root.findall("gpu"):
        uuid = (gpu.findtext("uuid") or "").strip()
        if uuid not in indices:
            continue
        for process in gpu.findall("processes/process_info"):
            pid = (process.findtext("pid") or "").strip()
            if not pid.isdigit() or int(pid) <= 0:
                continue
            memory_fields = (process.findtext("used_memory") or "").split()
            memory = None
            if len(memory_fields) == 2 and memory_fields[1] == "MiB":
                try:
                    value = float(memory_fields[0])
                    if math.isfinite(value) and value >= 0:
                        memory = value * 1024 ** 2
                except ValueError:
                    pass
            processes.setdefault(int(pid), []).append({"index": indices[uuid], "memory_bytes": memory})
    return processes


class ProcessService:
    def __init__(self, task_provider, *, gpu_process_provider=gpu_processes, interval=2.0):
        self.task_provider = task_provider
        self.gpu_process_provider = gpu_process_provider
        self.interval = interval
        self._lock = threading.RLock()
        self._previous = {}
        self._cached: dict | None = None
        self._sampled = 0.0
        self._users = {}
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.uid = os.geteuid()

    def _protected(self):
        return {1, os.getpid(), *(p.pid for p in psutil.Process().parents())}

    def _user(self, uid):
        if uid not in self._users:
            try:
                self._users[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self._users[uid] = str(uid)
        return self._users[uid]

    def snapshot(self, gpu_indices=None):
        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._sampled < self.interval:
                return self._cached
            rows, previous = [], {}
            protected, total_memory = self._protected(), psutil.virtual_memory().total
            gpu_error = None
            try:
                gpu_map = self.gpu_process_provider(gpu_indices or {})
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                gpu_map, gpu_error = {}, f"GPU 进程指标暂不可用：{exc}"
            for p in psutil.process_iter():
                try:
                    # Reuse cached metadata, but check the kernel identity on each
                    # scan so PID reuse cannot retain a previous start time.
                    if not p.is_running():
                        continue
                    with p.oneshot():
                        created, uids = p.create_time(), p.uids()
                        cpu = p.cpu_times()
                        memory = p.memory_info()
                        try:
                            io = p.io_counters()
                        except (psutil.AccessDenied, psutil.ZombieProcess):
                            io = None
                        try:
                            cmd = p.cmdline()
                        except (psutil.AccessDenied, psutil.ZombieProcess):
                            cmd = []
                        identity = (p.pid, created)
                        counters = (now, cpu.user + cpu.system, io.read_bytes if io else None, io.write_bytes if io else None)
                        old = self._previous.get(identity)
                        elapsed = now - old[0] if old else 0
                        def rate(index, scale=1):
                            return round(max(0, counters[index] - old[index]) / elapsed * scale, 2) if old and elapsed > 0 and counters[index] is not None and old[index] is not None else None
                        previous[identity] = counters
                        reason = "服务及其父进程受保护" if p.pid in protected else "仅允许操作服务运行用户的进程" if uids.real != self.uid or uids.effective != self.uid else None
                        rows.append({"pid": p.pid, "ppid": p.ppid(), "create_time": created,
                                     "uid": uids.real, "user": self._user(uids.real), "name": p.name(),
                                     "command": " ".join(cmd) or p.name(), "status": p.status(),
                                     "cpu_percent": rate(1, 100), "cpu_seconds": cpu.user + cpu.system,
                                     "rss_bytes": memory.rss, "vms_bytes": memory.vms,
                                     "memory_percent": round(memory.rss / total_memory * 100, 2),
                                     "elapsed_seconds": max(0, time.time() - created),
                                     "threads": p.num_threads(), "nice": p.nice(),
                                     "read_bytes_per_second": rate(2), "write_bytes_per_second": rate(3),
                                     "gpus": gpu_map.get(p.pid, []), "task": None,
                                     "can_signal": reason is None, "signal_disabled_reason": reason})
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ProcessLookupError):
                    continue
            by_pid = {row["pid"]: row for row in rows}
            tasks = {}
            for task in self.task_provider():
                row = by_pid.get(task.get("pid"))
                # A stale task PID must not label a newly reused process.
                started = datetime.fromisoformat(task["started_at"]).timestamp() if task.get("started_at") else None
                if row and (started is None or abs(row["create_time"] - started) < 10):
                    tasks[row["pid"]] = {"id": task["id"], "name": task["name"]}
            for row in rows:
                cursor, seen = row["pid"], set()
                while cursor in by_pid and cursor not in seen:
                    if cursor in tasks:
                        row["task"] = tasks[cursor]
                        break
                    seen.add(cursor)
                    cursor = by_pid[cursor]["ppid"]
            self._previous, self._sampled = previous, time.monotonic()
            self._cached = {"processes": rows, "sampled_at": time.time(), "boot_id": self.boot_id,
                            "service_uid": self.uid, "gpu_error": gpu_error}
            return self._cached

    def query(self, *, gpu_indices=None, search="", user="", status="", scope="all", gpu=None,
              sort="cpu_percent", descending=True, offset=0, limit=100, tree=False):
        snapshot = self.snapshot(gpu_indices)
        rows = snapshot["processes"]
        search = search.casefold().strip()
        matched = [row for row in rows
                   if (not search or (row["pid"] == int(search) if search.isdigit() else search in row["command"].casefold() or search in row["name"].casefold()))
                   and (not user or row["user"] == user)
                   and (not status or row["status"] == status)
                   and (scope != "mine" or row["uid"] == self.uid)
                   and (scope != "gpu" or bool(row["gpus"]))
                   and (scope != "tasks" or row["task"] is not None)
                   and (gpu is None or any(item["index"] == gpu for item in row["gpus"]))]
        known = [row for row in matched if row[sort] is not None]
        unknown = [row for row in matched if row[sort] is None]
        matched = sorted(known, key=lambda row: (row[sort], row["pid"]), reverse=descending) + sorted(unknown, key=lambda row: row["pid"])
        page = [dict(row, context=False) for row in matched[offset:offset + limit]]
        if tree:
            by_pid, included = {row["pid"]: row for row in rows}, {row["pid"] for row in page}
            for row in list(page):
                parent, seen = row["ppid"], set()
                while parent in by_pid and parent not in seen and parent not in included:
                    seen.add(parent)
                    included.add(parent)
                    page.append(dict(by_pid[parent], context=True))
                    parent = by_pid[parent]["ppid"]
        return {key: value for key, value in snapshot.items() if key != "processes"} | {
            "items": page, "total": len(matched), "all_total": len(rows),
            "gpu_indices": sorted(set((gpu_indices or {}).values())),
            "users": sorted({row["user"] for row in rows}), "statuses": sorted({row["status"] for row in rows})}

    def _checked_process(self, pid, create_time, boot_id):
        if boot_id != self.boot_id:
            raise ValueError("主机已重启，请刷新后重试")
        p = psutil.Process(pid)
        if not math.isfinite(create_time) or abs(p.create_time() - create_time) > .001:
            raise ValueError("PID 已被其他进程复用，请刷新后重试")
        return p

    def detail(self, pid, create_time, boot_id):
        p = self._checked_process(pid, create_time, boot_id)
        with p.oneshot():
            data = {"pid": pid, "create_time": create_time, "name": p.name(), "status": p.status(), "ppid": p.ppid()}
            for key, reader in (("command", lambda: " ".join(p.cmdline())), ("cwd", p.cwd), ("exe", p.exe),
                                ("nice", p.nice), ("threads", p.num_threads), ("file_descriptors", p.num_fds)):
                try:
                    data[key] = reader()
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    data[key] = None
            return data

    def send_signal(self, targets, action, boot_id):
        if action not in SIGNALS:
            raise ValueError("不支持的进程操作")
        results, seen = [], set()
        for target in targets:
            pid, created = target["pid"], target["create_time"]
            result = {"pid": pid, "create_time": created, "action": action, "ok": False}
            fd = None
            try:
                if pid <= 1 or pid in self._protected():
                    raise PermissionError("服务及其父进程受保护")
                if pid in seen:
                    raise ValueError("重复的 PID")
                seen.add(pid)
                # Hold a kernel process identity while checking user and start time.
                if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                    fd = os.pidfd_open(pid)
                p = self._checked_process(pid, created, boot_id)
                uids = p.uids()
                if uids.real != self.uid or uids.effective != self.uid:
                    raise PermissionError("仅允许操作服务运行用户的进程")
                result["name"] = p.name()
                if fd is not None:
                    signal.pidfd_send_signal(fd, SIGNALS[action])
                else:
                    p.send_signal(SIGNALS[action])
                result.update(ok=True, message="信号已发送，等待进程状态更新")
            except (psutil.NoSuchProcess, ProcessLookupError):
                result["message"] = "进程已退出"
            except (psutil.AccessDenied, PermissionError):
                result["message"] = "无操作权限或进程受保护"
            except (OSError, ValueError) as exc:
                result["message"] = str(exc)
            finally:
                if fd is not None:
                    os.close(fd)
            results.append(result)
        with self._lock:
            self._sampled = 0
        return results
