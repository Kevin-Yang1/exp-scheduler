from __future__ import annotations

import asyncio
import errno
import logging
import os
from pathlib import Path
import pty
import signal
import subprocess
import traceback

from .terminal import (
    DEFAULT_TERMINAL_COLUMNS,
    DEFAULT_TERMINAL_ROWS,
    TERMINAL_CHUNK_BYTES,
    TERMINAL_SNAPSHOT_BYTES,
    TerminalSession,
    TerminalSubscriber,
    encode_terminal_text,
    set_terminal_window_size,
)


LOGGER = logging.getLogger("exp_scheduler")
SYSTEM_TERMINATE_GRACE_SECONDS = 3
FULLSCREEN_CLEAR_BYTES = b"\x1b[2J\x1b[H"
TERMINAL_REDRAW_BYTES = b"\x0c"


class NvitopTerminalService:
    def __init__(
        self,
        *,
        state_dir: Path,
        command: str = "nvitop",
    ) -> None:
        self.state_dir = state_dir
        self.command = command
        self._lock = asyncio.Lock()
        self._session: TerminalSession | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._watch_task: asyncio.Task[None] | None = None

    async def subscribe(
        self,
        *,
        cols: int | None = None,
        rows: int | None = None,
    ) -> tuple[TerminalSubscriber, bytes]:
        if self._is_fullscreen_nvitop():
            await self.shutdown()
            subscriber = TerminalSubscriber()
            async with self._lock:
                await self._start_locked(
                    initial_subscribers={subscriber},
                    cols=cols or DEFAULT_TERMINAL_COLUMNS,
                    rows=rows or DEFAULT_TERMINAL_ROWS,
                )
                return subscriber, FULLSCREEN_CLEAR_BYTES

        async with self._lock:
            if self._session is None or self._session.closed or self._process_exited():
                await self._start_locked(
                    cols=cols or DEFAULT_TERMINAL_COLUMNS,
                    rows=rows or DEFAULT_TERMINAL_ROWS,
                )
            if self._session is None:
                raise ValueError("nvitop 终端不可用")
            return self._session.subscribe(snapshot_bytes=TERMINAL_SNAPSHOT_BYTES)

    async def unsubscribe(self, subscriber: TerminalSubscriber) -> None:
        should_shutdown = False
        async with self._lock:
            if self._session is None:
                return
            self._session.unsubscribe(subscriber)
            should_shutdown = not self._session.subscribers
        if should_shutdown:
            await self.shutdown()

    async def resize(self, *, cols: int, rows: int) -> None:
        async with self._lock:
            if self._session is None or self._session.closed:
                raise ValueError("nvitop 终端不可用")
            try:
                self._session.resize(cols=cols, rows=rows)
                if self._is_fullscreen_nvitop():
                    self._request_repaint_locked()
            except OSError as exc:
                raise ValueError("nvitop 终端不可用") from exc

    async def shutdown(self) -> None:
        async with self._lock:
            session = self._session
            process = self._process
            watch_task = self._watch_task
        if session is None:
            return
        if process is not None and process.poll() is None:
            await self._terminate_process(process)
        if watch_task is not None and watch_task is not asyncio.current_task():
            await asyncio.gather(watch_task, return_exceptions=True)
        elif not session.closed:
            await self._close_session(session, exit_payload={"source": "nvitop", "status": "stopped"})
        async with self._lock:
            if self._session is session:
                self._session = None
                self._process = None
                self._watch_task = None

    def _process_exited(self) -> bool:
        return self._process is not None and self._process.poll() is not None

    def _is_fullscreen_nvitop(self) -> bool:
        return self.command.strip() == "nvitop"

    def _request_repaint_locked(self) -> None:
        if self._session is None or self._session.master_fd < 0 or self._process_exited():
            return
        try:
            os.write(self._session.master_fd, TERMINAL_REDRAW_BYTES)
        except OSError:
            LOGGER.debug("Failed to request nvitop repaint", exc_info=True)

    async def _start_locked(
        self,
        *,
        initial_subscribers: set[TerminalSubscriber] | None = None,
        cols: int = DEFAULT_TERMINAL_COLUMNS,
        rows: int = DEFAULT_TERMINAL_ROWS,
    ) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.state_dir / "nvitop-terminal.log"
        log_file = open(log_path, "w+b")
        master_fd, slave_fd = pty.openpty()
        terminal_cols, terminal_rows = set_terminal_window_size(
            slave_fd,
            cols=cols,
            rows=rows,
        )
        session = TerminalSession(
            task_id=0,
            master_fd=master_fd,
            log_path=log_path,
            log_file=log_file,
            cols=terminal_cols,
            rows=terminal_rows,
        )
        if initial_subscribers:
            session.subscribers.update(initial_subscribers)
        session.append_bytes(encode_terminal_text("[exp-scheduler] launching nvitop\n"))

        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLUMNS", str(terminal_cols))
        env.setdefault("LINES", str(terminal_rows))
        env.setdefault("PYTHONUNBUFFERED", "1")
        launch_command = self._build_launch_command()

        try:
            process = subprocess.Popen(
                ["bash", "-lc", launch_command],
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                text=False,
            )
        except Exception as exc:
            session.append_bytes(encode_terminal_text("[exp-scheduler] launch failed\n"))
            session.append_bytes(encode_terminal_text("".join(traceback.format_exception(exc))))
            self._close_master_fd(session)
            log_file.close()
            os.close(slave_fd)
            raise ValueError(f"nvitop 启动失败: {exc}") from exc
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass

        session.reader_task = asyncio.create_task(
            self._read_terminal_output(session),
            name="nvitop-terminal-reader",
        )
        self._session = session
        self._process = process
        self._watch_task = asyncio.create_task(
            self._watch_process(process, session),
            name="nvitop-terminal-watch",
        )

    def _build_launch_command(self) -> str:
        if self.command.strip() == "nvitop":
            return (
                "if command -v nvitop >/dev/null 2>&1; then "
                "exec nvitop; "
                "else "
                "printf '[exp-scheduler] nvitop command not found in PATH\\r\\n'; "
                "printf '[exp-scheduler] install nvitop or expose it to this service environment\\r\\n'; "
                "exit 127; "
                "fi"
            )
        return f"exec {self.command}"

    async def _read_terminal_output(self, session: TerminalSession) -> None:
        while True:
            try:
                data = await asyncio.to_thread(os.read, session.master_fd, TERMINAL_CHUNK_BYTES)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    break
                LOGGER.warning("Failed to read nvitop PTY output: %s", exc)
                break
            if not data:
                break
            session.append_bytes(data)

    async def _watch_process(
        self,
        process: subprocess.Popen[bytes],
        session: TerminalSession,
    ) -> None:
        exit_payload: dict[str, object] | None = None
        try:
            exit_code = await asyncio.to_thread(process.wait)
            if session.reader_task is not None:
                await session.reader_task
                session.reader_task = None
            status = "succeeded" if exit_code == 0 else "failed"
            if not session.closed:
                session.append_bytes(
                    encode_terminal_text(
                        f"\n[exp-scheduler] nvitop exited status={status} exit_code={exit_code}\n"
                    )
                )
            exit_payload = {
                "source": "nvitop",
                "status": status,
                "exit_code": exit_code,
            }
        finally:
            await self._close_session(session, exit_payload=exit_payload)
            async with self._lock:
                if self._session is session:
                    self._session = None
                    self._process = None
                    self._watch_task = None

    async def _close_session(
        self,
        session: TerminalSession,
        *,
        exit_payload: dict[str, object] | None,
    ) -> None:
        if exit_payload is not None and not session.closed:
            session.publish_exit(exit_payload)
        elif exit_payload is None:
            session.closed = True
            for subscriber in list(session.subscribers):
                subscriber.control_queue.put_nowait(("disconnect", None))
            session.subscribers.clear()
        self._close_master_fd(session)
        if session.reader_task is not None and session.reader_task is not asyncio.current_task():
            await asyncio.gather(session.reader_task, return_exceptions=True)
            session.reader_task = None
        if not session.log_file.closed:
            session.log_file.flush()
            session.log_file.close()

    async def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(
                asyncio.to_thread(process.wait),
                timeout=SYSTEM_TERMINATE_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            await asyncio.to_thread(process.wait)

    def _close_master_fd(self, session: TerminalSession) -> None:
        if session.master_fd < 0:
            return
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        session.master_fd = -1
