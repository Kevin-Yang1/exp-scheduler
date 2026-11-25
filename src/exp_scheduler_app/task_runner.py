from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios


BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
POLL_SECONDS = 0.1
RUNNER_PATH = Path(__file__).resolve()


def process_start_time(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return fields[19] if fields[0] not in {"Z", "X"} else None


def control_path(log_path: Path, kind: str) -> Path:
    return log_path.with_name(f".{log_path.name}.{kind}.json")


def write_control(log_path: Path, kind: str, payload: dict) -> None:
    target = control_path(log_path, kind)
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(target)


def read_control(log_path: Path, kind: str) -> dict:
    try:
        return json.loads(control_path(log_path, kind).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


@dataclass(slots=True)
class TaskRun:
    log_path: Path
    pid: int
    task_start_time: str
    runner_pid: int
    runner_start_time: str
    launcher: subprocess.Popen[bytes] | None = None

    @classmethod
    def recover(cls, log_path: Path, pid: int) -> TaskRun | None:
        data = read_control(log_path, "process")
        if not data:
            return None
        if (
            data.get("pid") != pid
            or data.get("boot_id") != BOOT_ID_PATH.read_text().strip()
            or not isinstance(data.get("task_start_time"), str)
            or not isinstance(data.get("runner_start_time"), str)
            or not isinstance(data.get("runner_pid"), int)
        ):
            return None
        return cls(log_path, pid, data["task_start_time"], data["runner_pid"], data["runner_start_time"])

    @classmethod
    def prepare(
        cls,
        log_path: Path,
        *,
        command: str,
        cwd: str | None,
        env: dict[str, str],
        cols: int,
        rows: int,
    ) -> TaskRun:
        for kind in ("process", "exit", "stop", "size"):
            control_path(log_path, kind).unlink(missing_ok=True)
        with log_path.open("ab") as log:
            launcher = subprocess.Popen(
                [sys.executable, "-I", "-B", str(RUNNER_PATH), str(log_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                start_new_session=True,
            )
        assert launcher.stdin is not None and launcher.stdout is not None
        try:
            spec = dict(command=command, cwd=cwd, env=env, cols=cols, rows=rows)
            launcher.stdin.write(json.dumps(spec).encode() + b"\n")
            launcher.stdin.flush()
            if not select.select([launcher.stdout], [], [], 5)[0]:
                raise TimeoutError("Task runner did not initialize its terminal")
            response = json.loads(launcher.stdout.readline() or b"{}")
            if "pid" not in response:
                raise RuntimeError(response.get("error", "Task runner exited during startup"))
            run = cls.recover(log_path, response["pid"])
            if run is None:
                raise RuntimeError("Could not identify task runner")
            run.launcher = launcher
            return run
        except BaseException:
            with suppress(BrokenPipeError):
                launcher.stdin.close()
            try:
                launcher.wait(timeout=1)
            except subprocess.TimeoutExpired:
                launcher.kill()
                launcher.wait()
            raise
        finally:
            launcher.stdout.close()

    def release(self, *, start: bool) -> None:
        assert self.launcher is not None and self.launcher.stdin is not None
        with self.launcher.stdin as gate:
            if start:
                gate.write(b"start\n")

    def task_alive(self) -> bool:
        return process_start_time(self.pid) == self.task_start_time

    def active(self) -> bool:
        if self.launcher is not None:
            runner_alive = self.launcher.poll() is None
        else:
            runner_alive = process_start_time(self.runner_pid) == self.runner_start_time
        return runner_alive or self.task_alive()

    def send_signal(self, sig: signal.Signals) -> None:
        if not self.task_alive():
            return
        try:
            os.killpg(self.pid, sig)
        except ProcessLookupError:
            pass

    def exit_code(self) -> int:
        result = read_control(self.log_path, "exit")
        if result.get("runner_pid") != self.runner_pid:
            return -signal.SIGKILL
        return int(result["code"])

    def cleanup(self) -> None:
        for kind in ("process", "exit", "stop", "size"):
            control_path(self.log_path, kind).unlink(missing_ok=True)


def resize_pty(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def run_task(log_path: Path) -> None:
    spec = json.loads(sys.stdin.buffer.readline())
    master, slave = pty.openpty()
    gate_read, gate_write = os.pipe()
    with log_path.open("ab", buffering=0) as log:
        try:
            resize_pty(slave, spec["cols"], spec["rows"])
            # The command starts only after the scheduler records its PID in SQLite.
            gate_command = f"read -r <&{gate_read} || exit 125; exec {gate_read}<&-; exec bash -lc \"$1\""
            task = subprocess.Popen(
                ["bash", "-pc", gate_command, "exp-scheduler", spec["command"]],
                cwd=spec["cwd"], env=spec["env"], stdin=slave, stdout=slave, stderr=slave,
                pass_fds=(gate_read,), start_new_session=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(gate_write)
            print(json.dumps({"error": str(exc)}), flush=True)
            return
        finally:
            os.close(slave)
            os.close(gate_read)

        try:
            write_control(log_path, "process", {
                "boot_id": BOOT_ID_PATH.read_text().strip(),
                "pid": task.pid,
                "task_start_time": process_start_time(task.pid),
                "runner_pid": os.getpid(),
                "runner_start_time": process_start_time(os.getpid()),
            })
            print(json.dumps({"pid": task.pid}), flush=True)
            started = sys.stdin.buffer.readline() == b"start\n"
            if started:
                with suppress(BrokenPipeError):
                    os.write(gate_write, b"start\n")
        finally:
            os.close(gate_write)
            sys.stdin.close()
            sys.stdout.close()

        size = {}
        while True:
            new_size = read_control(log_path, "size")
            if new_size and new_size != size:
                resize_pty(master, new_size["cols"], new_size["rows"])
                size = new_size
            if not select.select([master], [], [], POLL_SECONDS)[0]:
                continue
            try:
                data = os.read(master, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
            if not data:
                break
            log.write(data)
        os.close(master)
        code = task.wait()
        write_control(log_path, "exit", {
            "runner_pid": os.getpid(),
            "code": code if started else -signal.SIGTERM,
        })


if __name__ == "__main__":
    run_task(Path(sys.argv[1]))
