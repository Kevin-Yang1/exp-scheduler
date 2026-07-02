from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import ctypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
import errno
import logging
import os
from pathlib import Path
import pty
import secrets
import shutil
import shlex
import signal
import subprocess
import time
from typing import TYPE_CHECKING

from .terminal import (
    DEFAULT_TERMINAL_COLUMNS,
    DEFAULT_TERMINAL_ROWS,
    TERMINAL_CHUNK_BYTES,
    TerminalSubscriber,
    encode_terminal_text,
    set_terminal_window_size,
)
from .tmux_utils import (
    REMOTE_TERMINAL_LOG_DIR,
    TMUX_SESSION_PREFIX,
    build_remote_tmux_attach_argv,
    build_remote_tmux_new_script,
    build_tmux_attach_command,
    build_tmux_has_session_command,
    build_tmux_new_command,
    build_tmux_resize_command,
    build_tmux_setup_command,
    ensure_remote_tmux,
    remote_tmux_capture_pane,
    remote_tmux_disable_status,
    remote_tmux_has_session,
    remote_tmux_kill_session,
    remote_tmux_paste_input,
    remote_tmux_resize,
    tmux_capture_pane,
    tmux_disable_status,
    tmux_has_session,
    tmux_kill_session,
    tmux_list_sessions,
    tmux_paste_input,
    tmux_resize_window,
    tmux_socket_name,
)
from .nodes import build_ssh_command

if TYPE_CHECKING:
    from .nodes import ResolvedAuth


LOGGER = logging.getLogger("exp_scheduler")

INTERACTIVE_TERMINATE_GRACE_SECONDS = 3
MAX_INPUT_BYTES = 64 * 1024
READER_DRAIN_GRACE_SECONDS = 1.0
INPUT_WRITE_TIMEOUT_SECONDS = 2.0
INPUT_WRITE_RETRY_INTERVAL_SECONDS = 0.05
SNAPSHOT_CHUNK_BYTES = 64 * 1024
LOG_ROTATION_INTERVAL_SECONDS = 300.0


@dataclass(slots=True, eq=False)
class InteractiveSession:
    terminal_id: str
    name: str
    node_id: str
    node_name: str
    is_local: bool
    tmux_session: str
    tmux_socket: str
    master_fd: int
    process: subprocess.Popen[bytes]
    subscribers: set[TerminalSubscriber]
    watch_task: asyncio.Task[None] | None
    created_at: float
    last_activity_at: float
    log_path: Path
    remote_log_path: str | None = None
    ssh_prefix: list[str] | None = None
    ssh_env: dict[str, str] | None = None
    exit_code: int | None = None
    exit_reason: str | None = None
    reader_done: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    detaching: bool = False
    cols: int = DEFAULT_TERMINAL_COLUMNS
    rows: int = DEFAULT_TERMINAL_ROWS

    def add_subscriber(self) -> TerminalSubscriber:
        subscriber = TerminalSubscriber()
        self.subscribers.add(subscriber)
        return subscriber

    def remove_subscriber(self, subscriber: TerminalSubscriber) -> None:
        self.subscribers.discard(subscriber)

    def append_bytes(self, data: bytes) -> None:
        if self.closed or not data:
            return
        for subscriber in list(self.subscribers):
            if subscriber.chunk_queue.full():
                subscriber.control_queue.put_nowait(("disconnect", None))
                self.subscribers.discard(subscriber)
                continue
            subscriber.chunk_queue.put_nowait(data)

    def publish_exit(self, payload: dict[str, object]) -> None:
        if self.closed:
            return
        self.closed = True
        for subscriber in list(self.subscribers):
            subscriber.control_queue.put_nowait(("exit", payload))
        self.subscribers.clear()


class InteractiveTerminalService:
    """tmux-backed 持久交互终端服务。

    每个终端 backed by 一个 tmux session（独立 socket -L 隔离）。
    pipe-pane 持续将原始 PTY 字节流写入日志文件（即使无人 attach）。
    SSE 断开仅移除订阅者，attach reader 保留以维持 TUI 状态与输出连续性。
    服务重启后通过 _reconcile_on_startup 重建 attach reader。
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        terminal_log_dir: Path,
        events,
        node_resolver: Callable[[str], ResolvedAuth],
        database=None,
        history_limit: int = 100000,
        max_log_mb: int = 200,
        remote_max_log_mb: int = 200,
        max_sessions: int = 16,
        known_hosts_path: Path | None = None,
        ssh_binary: str = "ssh",
        sshpass_binary: str = "sshpass",
    ) -> None:
        self.state_dir = state_dir
        self.terminal_log_dir = terminal_log_dir
        self.events = events
        self.node_resolver = node_resolver
        self.database = database
        self.history_limit = history_limit
        self.max_log_mb = max_log_mb
        self.remote_max_log_mb = remote_max_log_mb
        self.max_sessions = max_sessions
        self.known_hosts_path = known_hosts_path
        self.ssh_binary = ssh_binary
        self.sshpass_binary = sshpass_binary
        self.tmux_socket = tmux_socket_name(state_dir)
        self._sessions: dict[str, InteractiveSession] = {}
        self._lock = asyncio.Lock()
        self._rotation_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._live_dir = terminal_log_dir / "live"
        self._archived_dir = terminal_log_dir / "archived"
        self._live_dir.mkdir(parents=True, exist_ok=True)
        self._archived_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def create_session(
        self,
        node_id: str,
        cols: int | None = None,
        rows: int | None = None,
        name: str | None = None,
    ) -> dict[str, object]:
        if self._shutting_down:
            raise ValueError("终端服务正在关闭，无法创建新会话")
        auth = self.node_resolver(node_id)
        terminal_id = secrets.token_urlsafe(8)
        tmux_session = f"{TMUX_SESSION_PREFIX}{terminal_id}"
        display_name = name or f"{auth.name} {terminal_id[:4]}"
        c = cols or DEFAULT_TERMINAL_COLUMNS
        r = rows or DEFAULT_TERMINAL_ROWS

        async with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise ValueError(
                    f"交互终端数已达上限（{self.max_sessions}），请先关闭闲置会话"
                )
            if auth.is_local:
                log_path = self._live_dir / f"{terminal_id}.stream.log"
                self._create_local_tmux_session(
                    tmux_session=tmux_session,
                    cols=c,
                    rows=r,
                    log_path=log_path,
                )
                ssh_prefix = None
                ssh_env = None
                remote_log_path = None
            else:
                ssh_prefix, ssh_env = self._build_ssh_prefix(auth)
                remote_log_path = f"{REMOTE_TERMINAL_LOG_DIR}/{tmux_session}.stream.log"
                self._create_remote_tmux_session(
                    ssh_prefix=ssh_prefix,
                    ssh_env=ssh_env,
                    auth=auth,
                    tmux_session=tmux_session,
                    cols=c,
                    rows=r,
                    remote_log_path=remote_log_path,
                )
                log_path = self._live_dir / f"{terminal_id}.stream.log"
                log_path.touch(mode=0o600, exist_ok=True)

            session = self._start_attach_reader(
                terminal_id=terminal_id,
                name=display_name,
                auth=auth,
                tmux_session=tmux_session,
                log_path=log_path,
                cols=c,
                rows=r,
                ssh_prefix=ssh_prefix,
                ssh_env=ssh_env,
                remote_log_path=remote_log_path,
            )
            self._sessions[terminal_id] = session

        if self.database is not None:
            self.database.create_terminal(
                terminal_id=terminal_id,
                name=display_name,
                node_id=auth.node_id,
                node_name=auth.name,
                is_local=auth.is_local,
                tmux_session=tmux_session,
                log_path=str(log_path),
                remote_log_path=remote_log_path,
            )
        self._ensure_rotation()
        await self.events.publish(
            "terminal_session_created",
            {
                "session_id": terminal_id,
                "node_id": session.node_id,
                "node_name": session.node_name,
                "name": display_name,
            },
        )
        return self._session_info(session)

    async def list_sessions(self) -> list[dict[str, object]]:
        async with self._lock:
            sessions = sorted(self._sessions.values(), key=lambda s: s.created_at)
            return [self._session_info(session) for session in sessions]

    async def subscribe(
        self,
        session_id: str,
        cols: int | None = None,
        rows: int | None = None,
    ) -> tuple[TerminalSubscriber, bytes, dict[str, object]]:
        async with self._lock:
            session = self._get_session(session_id)
            if (
                not session.closed
                and (session.master_fd < 0 or session.process.poll() is not None)
            ):
                session = self._restart_attach_reader_locked(
                    session,
                    cols=cols or session.cols,
                    rows=rows or session.rows,
                )
        if session.closed:
            raise ValueError(f"终端会话 {session_id} 已退出")
        if cols is not None and rows is not None:
            await self.resize(session_id, cols=cols, rows=rows)
        snapshot = self._capture_snapshot(session)
        subscriber = session.add_subscriber()
        session.last_activity_at = time.time()
        if self.database is not None:
            self.database.touch_terminal(session_id)
        return subscriber, snapshot, self._session_info(session)

    async def unsubscribe(self, session_id: str, subscriber: TerminalSubscriber) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.remove_subscriber(subscriber)
        session.last_activity_at = time.time()

    async def write_input(self, session_id: str, data: bytes) -> None:
        async with self._lock:
            session = self._get_session(session_id)
            if (
                not session.closed
                and (session.master_fd < 0 or session.process.poll() is not None)
            ):
                session = self._restart_attach_reader_locked(
                    session,
                    cols=session.cols,
                    rows=session.rows,
                )
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError(f"单次输入超过 {MAX_INPUT_BYTES // 1024}KB 上限")
        if session.closed or session.process.poll() is not None:
            raise ValueError(f"终端会话 {session_id} 已退出")
        if data:
            await self._write_input_bytes(session, data)
        session.last_activity_at = time.time()

    async def write_startup_input(self, session_id: str, data: bytes) -> None:
        """Inject startup input directly into tmux, independent of attach timing."""
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError(f"单次输入超过 {MAX_INPUT_BYTES // 1024}KB 上限")
        async with self._lock:
            session = self._get_session(session_id)
        if session.closed:
            raise ValueError(f"终端会话 {session_id} 已退出")
        if data:
            if session.is_local:
                await asyncio.to_thread(
                    tmux_paste_input,
                    session.tmux_socket,
                    session.tmux_session,
                    data,
                )
            else:
                await asyncio.to_thread(
                    remote_tmux_paste_input,
                    session.ssh_prefix or [],
                    session.ssh_env or {},
                    session_name=session.tmux_session,
                    data=data,
                )
        session.last_activity_at = time.time()

    async def _write_input_bytes(self, session: InteractiveSession, data: bytes) -> None:
        view = memoryview(data)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INPUT_WRITE_TIMEOUT_SECONDS
        while True:
            async with self._lock:
                if session.closed or session.master_fd < 0:
                    raise ValueError(f"终端会话 {session.terminal_id} 已退出")
                try:
                    written = os.write(session.master_fd, view)
                except BlockingIOError:
                    written = 0
                except OSError as exc:
                    raise ValueError(
                        f"终端会话 {session.terminal_id} 已退出"
                    ) from exc
            view = view[written:]
            if not view:
                return
            if loop.time() >= deadline:
                raise ValueError("终端输入缓冲区已满")
            await asyncio.sleep(INPUT_WRITE_RETRY_INTERVAL_SECONDS)

    async def resize(self, session_id: str, *, cols: int, rows: int) -> None:
        session = self._get_session(session_id)
        if session.closed:
            raise ValueError(f"终端会话 {session_id} 已退出")
        try:
            set_terminal_window_size(session.master_fd, cols=cols, rows=rows)
        except OSError:
            pass
        self._notify_attach_client_resized(session)
        session.cols = cols
        session.rows = rows
        if session.is_local:
            tmux_resize_window(
                session.tmux_socket, session.tmux_session, cols=cols, rows=rows
            )
        else:
            remote_tmux_resize(
                session.ssh_prefix or [],
                session.ssh_env or {},
                session_name=session.tmux_session,
                cols=cols,
                rows=rows,
            )
        session.last_activity_at = time.time()

    def _notify_attach_client_resized(self, session: InteractiveSession) -> None:
        """Notify tmux attach that its PTY size changed.

        TIOCSWINSZ updates the PTY size, but the tmux client keeps its old
        dimensions until it receives SIGWINCH. If the tmux client and window
        sizes diverge, tmux draws only part of the screen and the browser xterm
        shows stale filler cells around it.
        """
        if session.process.poll() is not None:
            return
        try:
            os.killpg(session.process.pid, signal.SIGWINCH)
        except ProcessLookupError:
            return
        except OSError:
            LOGGER.debug(
                "Failed to signal terminal resize for %s",
                session.terminal_id,
                exc_info=True,
            )

    async def rename_session(self, session_id: str, name: str) -> dict[str, object]:
        session = self._get_session(session_id)
        name = name.strip()
        if not name:
            raise ValueError("终端名称不能为空")
        session.name = name
        if self.database is not None:
            self.database.rename_terminal(session_id, name)
        return self._session_info(session)

    async def close_session(self, session_id: str) -> dict[str, object]:
        session = self._get_session(session_id)
        await self._archive_session(session)
        info = self._session_info(session)
        info["status"] = "closed"
        return info

    async def list_archived(self) -> list[dict[str, object]]:
        if self.database is None:
            return []
        return self.database.list_terminals(status="closed")

    async def read_archived_log(
        self,
        terminal_id: str,
        *,
        tail_bytes: int | None = None,
        offset: int = 0,
    ) -> bytes:
        if self.database is None:
            return b""
        terminal = self.database.get_terminal(terminal_id)
        if terminal is None or terminal["status"] != "closed":
            return b""
        log_path = self._archived_dir / f"{terminal_id}.log"
        if not log_path.exists():
            return b""
        return self._read_log_range(log_path, tail_bytes=tail_bytes, offset=offset)

    async def read_live_log(
        self,
        terminal_id: str,
        *,
        tail_bytes: int | None = None,
        offset: int = 0,
    ) -> bytes:
        session = self._sessions.get(terminal_id)
        if session is not None and not session.closed:
            log_path = session.log_path
            if log_path.exists():
                return self._read_log_range(
                    log_path, tail_bytes=tail_bytes, offset=offset
                )
        if self.database is not None:
            terminal = self.database.get_terminal(terminal_id)
            if terminal is not None and terminal["status"] == "active":
                log_path = Path(terminal["log_path"])
                if log_path.exists():
                    return self._read_log_range(
                        log_path, tail_bytes=tail_bytes, offset=offset
                    )
        return b""

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._rotation_task is not None:
            self._rotation_task.cancel()
            await asyncio.gather(self._rotation_task, return_exceptions=True)
            self._rotation_task = None
        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                self._stop_attach_reader(session)
            except Exception:
                LOGGER.warning(
                    "Failed to stop attach reader %s during shutdown",
                    session.terminal_id,
                    exc_info=True,
                )

    async def reconcile_on_startup(self) -> None:
        """启动时校验 DB active 终端与 tmux session 的一致性。"""
        if self.database is None:
            return
        stored = self.database.get_terminal_log_dir()
        current = str(self.terminal_log_dir)
        has_records = bool(
            self.database.list_terminals(status="active")
            or self.database.list_terminals(status="closed")
        )
        if stored is not None and stored != current and has_records:
            raise ValueError(
                f"terminal_log_dir 已变更（旧: {stored} → 新: {current}），"
                "请停止服务并将旧目录的 live/ 和 archived/ 迁移到新路径后重启"
            )
        self.database.set_terminal_log_dir(current)

        active_terminals = self.database.list_terminals(status="active")
        for terminal in active_terminals:
            terminal_id = str(terminal["id"])
            tmux_session = str(terminal["tmux_session"])
            is_local = bool(terminal["is_local"])
            if is_local:
                exists = tmux_has_session(self.tmux_socket, tmux_session)
            else:
                ssh_prefix, ssh_env = self._build_ssh_prefix_from_stored(terminal)
                if ssh_prefix is None:
                    self.database.close_terminal(
                        terminal_id, exit_reason="node_unreachable"
                    )
                    continue
                exists = remote_tmux_has_session(
                    ssh_prefix, ssh_env, session_name=tmux_session
                )
            if exists:
                try:
                    self._rebuild_attach_reader(terminal)
                except Exception:
                    LOGGER.warning(
                        "Failed to rebuild attach reader for %s",
                        terminal_id,
                        exc_info=True,
                    )
                    self.database.close_terminal(
                        terminal_id, exit_reason="rebuild_failed"
                    )
            else:
                self._archive_terminal_files(terminal_id)
                self.database.close_terminal(
                    terminal_id, exit_reason="tmux_session_lost"
                )

        self._cleanup_orphaned_tmux_sessions(active_terminals)

    # ------------------------------------------------------------------
    # tmux session 创建（同步操作，在锁内调用）
    # ------------------------------------------------------------------

    def _create_local_tmux_session(
        self,
        *,
        tmux_session: str,
        cols: int,
        rows: int,
        log_path: Path,
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.touch(mode=0o600, exist_ok=True)
        new_cmd = build_tmux_new_command(
            self.tmux_socket,
            session_name=tmux_session,
            cols=cols,
            rows=rows,
            history_limit=self.history_limit,
            log_dir=log_path.parent,
        )
        result = subprocess.run(new_cmd, capture_output=True, timeout=10)
        if result.returncode != 0:
            raise ValueError(
                f"tmux 创建会话失败: {result.stderr.decode('utf-8', errors='replace')}"
            )
        setup_cmd = build_tmux_setup_command(
            self.tmux_socket,
            session_name=tmux_session,
            history_limit=self.history_limit,
            log_path=log_path,
        )
        setup_result = subprocess.run(setup_cmd, capture_output=True, timeout=10)
        if setup_result.returncode != 0:
            LOGGER.warning(
                "tmux setup failed for %s: %s",
                tmux_session,
                setup_result.stderr.decode("utf-8", errors="replace"),
            )

    def _create_remote_tmux_session(
        self,
        *,
        ssh_prefix: list[str],
        ssh_env: dict[str, str],
        auth: ResolvedAuth,
        tmux_session: str,
        cols: int,
        rows: int,
        remote_log_path: str,
    ) -> None:
        ensure_remote_tmux(
            ssh_prefix, ssh_env, node_name=auth.name, timeout=30
        )
        script = build_remote_tmux_new_script(
            session_name=tmux_session,
            cols=cols,
            rows=rows,
            history_limit=self.history_limit,
            remote_log_dir=REMOTE_TERMINAL_LOG_DIR,
        )
        result = subprocess.run(
            [*ssh_prefix, script],
            capture_output=True,
            env=ssh_env,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise ValueError(
                f"远端 tmux 创建会话失败（{auth.name}）: {stderr}"
            )

    # ------------------------------------------------------------------
    # attach reader（PTY 子进程跑 tmux attach）
    # ------------------------------------------------------------------

    def _start_attach_reader(
        self,
        *,
        terminal_id: str,
        name: str,
        auth: ResolvedAuth,
        tmux_session: str,
        log_path: Path,
        cols: int,
        rows: int,
        ssh_prefix: list[str] | None,
        ssh_env: dict[str, str] | None,
        remote_log_path: str | None,
    ) -> InteractiveSession:
        master_fd, slave_fd = pty.openpty()
        os.set_blocking(master_fd, False)
        terminal_cols, terminal_rows = set_terminal_window_size(
            slave_fd, cols=cols, rows=rows
        )
        if auth.is_local:
            argv = build_tmux_attach_command(
                self.tmux_socket, session_name=tmux_session
            )
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
        else:
            argv = build_remote_tmux_attach_argv(
                ssh_prefix or [], session_name=tmux_session
            )
            env = dict(ssh_env or {})
            env["TERM"] = "xterm-256color"

        try:
            process = subprocess.Popen(
                argv,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                text=False,
            )
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass

        now = time.time()
        session = InteractiveSession(
            terminal_id=terminal_id,
            name=name,
            node_id=auth.node_id,
            node_name=auth.name,
            is_local=auth.is_local,
            tmux_session=tmux_session,
            tmux_socket=self.tmux_socket,
            master_fd=master_fd,
            process=process,
            subscribers=set(),
            watch_task=None,
            created_at=now,
            last_activity_at=now,
            log_path=log_path,
            remote_log_path=remote_log_path,
            ssh_prefix=ssh_prefix,
            ssh_env=ssh_env,
            cols=terminal_cols,
            rows=terminal_rows,
        )
        asyncio.get_running_loop().add_reader(
            master_fd, self._on_master_readable, session
        )
        session.watch_task = asyncio.create_task(
            self._watch_process(session),
            name=f"interactive-terminal-watch-{terminal_id}",
        )
        return session

    def _rebuild_attach_reader(self, terminal: dict[str, object]) -> None:
        """从 DB 记录重建 attach reader（服务重启后调用）。"""
        terminal_id = str(terminal["id"])
        auth = self.node_resolver(str(terminal["node_id"]))
        log_path = Path(str(terminal["log_path"]))
        if not log_path.exists():
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(mode=0o600, exist_ok=True)
        if auth.is_local:
            ssh_prefix = None
            ssh_env = None
        else:
            ssh_prefix, ssh_env = self._build_ssh_prefix(auth)
        remote_log_path = terminal.get("remote_log_path")
        if auth.is_local:
            tmux_disable_status(self.tmux_socket, str(terminal["tmux_session"]))
        else:
            remote_tmux_disable_status(
                ssh_prefix or [],
                ssh_env or {},
                session_name=str(terminal["tmux_session"]),
            )
        session = self._start_attach_reader(
            terminal_id=terminal_id,
            name=str(terminal["name"]),
            auth=auth,
            tmux_session=str(terminal["tmux_session"]),
            log_path=log_path,
            cols=int(terminal.get("cols", DEFAULT_TERMINAL_COLUMNS))
            if "cols" in terminal
            else DEFAULT_TERMINAL_COLUMNS,
            rows=int(terminal.get("rows", DEFAULT_TERMINAL_ROWS))
            if "rows" in terminal
            else DEFAULT_TERMINAL_ROWS,
            ssh_prefix=ssh_prefix,
            ssh_env=ssh_env,
            remote_log_path=str(remote_log_path) if remote_log_path else None,
        )
        self._sessions[terminal_id] = session

    def _stop_attach_reader(self, session: InteractiveSession) -> None:
        """停止 attach PTY 子进程（tmux session 保留）。"""
        session.detaching = True
        if session.process.poll() is None:
            try:
                os.killpg(session.process.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        self._close_master_fd(session)

    def _restart_attach_reader_locked(
        self,
        session: InteractiveSession,
        *,
        cols: int,
        rows: int,
    ) -> InteractiveSession:
        auth = self.node_resolver(session.node_id)
        restarted = self._start_attach_reader(
            terminal_id=session.terminal_id,
            name=session.name,
            auth=auth,
            tmux_session=session.tmux_session,
            log_path=session.log_path,
            cols=cols,
            rows=rows,
            ssh_prefix=session.ssh_prefix,
            ssh_env=session.ssh_env,
            remote_log_path=session.remote_log_path,
        )
        restarted.created_at = session.created_at
        restarted.last_activity_at = session.last_activity_at
        self._sessions[session.terminal_id] = restarted
        return restarted

    def _on_master_readable(self, session: InteractiveSession) -> None:
        if session.closed or session.master_fd < 0:
            self._detach_reader(session)
            return
        while True:
            try:
                data = os.read(session.master_fd, TERMINAL_CHUNK_BYTES)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno not in {errno.EIO, errno.EBADF}:
                    LOGGER.warning("Failed to read attach PTY output: %s", exc)
                break
            if not data:
                break
            session.last_activity_at = time.time()
            session.append_bytes(data)
        self._detach_reader(session)

    def _detach_reader(self, session: InteractiveSession) -> None:
        if session.master_fd >= 0:
            with contextlib.suppress(RuntimeError, ValueError, OSError):
                asyncio.get_running_loop().remove_reader(session.master_fd)
        session.reader_done.set()

    async def _watch_process(self, session: InteractiveSession) -> None:
        exit_payload: dict[str, object] | None = None
        try:
            exit_code = await self._wait_process(session.process)
            session.exit_code = exit_code
            reason = "connection_lost" if exit_code == 255 else "exited"
            session.exit_reason = reason
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    session.reader_done.wait(),
                    timeout=READER_DRAIN_GRACE_SECONDS,
                )
            exit_payload = {
                "session_id": session.terminal_id,
                "node_id": session.node_id,
                "node_name": session.node_name,
                "status": "succeeded" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "reason": reason,
            }
        finally:
            if session.detaching and not session.closed:
                session.detaching = False
                session.watch_task = None
                session.exit_code = None
                session.exit_reason = None
                self._close_master_fd(session)
                return
            if exit_payload is not None and not session.closed:
                session.publish_exit(exit_payload)
            elif exit_payload is None and not session.closed:
                session.closed = True
                for subscriber in list(session.subscribers):
                    subscriber.control_queue.put_nowait(("disconnect", None))
                session.subscribers.clear()
            self._close_master_fd(session)
            async with self._lock:
                self._sessions.pop(session.terminal_id, None)
            if exit_payload is not None:
                await self.events.publish("terminal_session_closed", exit_payload)

    async def _wait_process(self, process: subprocess.Popen[bytes]) -> int:
        if process.poll() is not None:
            return int(process.returncode)
        try:
            pidfd = _pidfd_open(process.pid)
        except OSError:
            return await asyncio.to_thread(process.wait)
        loop = asyncio.get_running_loop()
        exited: asyncio.Future[None] = loop.create_future()

        def _on_pidfd_ready() -> None:
            if not exited.done():
                exited.set_result(None)

        loop.add_reader(pidfd, _on_pidfd_ready)
        try:
            await exited
        finally:
            with contextlib.suppress(RuntimeError, ValueError, OSError):
                loop.remove_reader(pidfd)
            os.close(pidfd)
        return process.wait()

    def _close_master_fd(self, session: InteractiveSession) -> None:
        if session.master_fd < 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            with contextlib.suppress(RuntimeError, ValueError, OSError):
                loop.remove_reader(session.master_fd)
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        session.master_fd = -1

    # ------------------------------------------------------------------
    # snapshot（capture-pane）
    # ------------------------------------------------------------------

    def _capture_snapshot(self, session: InteractiveSession) -> bytes:
        try:
            if session.is_local:
                snapshot = tmux_capture_pane(
                    session.tmux_socket, session.tmux_session
                )
            else:
                snapshot = remote_tmux_capture_pane(
                    session.ssh_prefix or [],
                    session.ssh_env or {},
                    session_name=session.tmux_session,
                )
            return self._normalize_snapshot_line_feeds(snapshot)
        except Exception:
            LOGGER.debug("Failed to capture pane for snapshot", exc_info=True)
            return b""

    @staticmethod
    def _normalize_snapshot_line_feeds(data: bytes) -> bytes:
        if not data:
            return data
        output = bytearray()
        previous = -1
        for byte in data:
            if byte == 10 and previous != 13:
                output.append(13)
            output.append(byte)
            previous = byte
        return bytes(output)

    # ------------------------------------------------------------------
    # 归档与日志
    # ------------------------------------------------------------------

    async def _archive_session(self, session: InteractiveSession) -> None:
        self._stop_attach_reader(session)
        if session.watch_task is not None and session.watch_task is not asyncio.current_task():
            await asyncio.gather(session.watch_task, return_exceptions=True)
        if session.is_local:
            tmux_kill_session(session.tmux_socket, session.tmux_session)
        else:
            remote_tmux_kill_session(
                session.ssh_prefix or [],
                session.ssh_env or {},
                session_name=session.tmux_session,
            )
        archived_path = self._archived_dir / f"{session.terminal_id}.log"
        if session.is_local:
            if session.log_path.exists():
                shutil.move(str(session.log_path), str(archived_path))
        else:
            self._scp_remote_log(session, archived_path)
        if self.database is not None:
            self.database.close_terminal(
                session.terminal_id,
                exit_code=session.exit_code,
                exit_reason=session.exit_reason or "closed",
            )
        async with self._lock:
            self._sessions.pop(session.terminal_id, None)
        await self.events.publish(
            "terminal_session_closed",
            {
                "session_id": session.terminal_id,
                "node_id": session.node_id,
                "node_name": session.node_name,
                "status": "closed",
                "reason": "closed",
            },
        )

    def _scp_remote_log(
        self, session: InteractiveSession, archived_path: Path
    ) -> None:
        if session.remote_log_path is None or session.ssh_prefix is None:
            return
        cat_cmd = [
            *session.ssh_prefix,
            f"cat {self._remote_path_arg(session.remote_log_path)}",
        ]
        try:
            with archived_path.open("wb") as fh:
                result = subprocess.run(
                    cat_cmd,
                    stdout=fh,
                    stderr=subprocess.PIPE,
                    timeout=60,
                    env=session.ssh_env,
                )
            if result.returncode != 0:
                with contextlib.suppress(OSError):
                    archived_path.unlink()
                LOGGER.warning(
                    "Failed to copy remote log for %s: %s",
                    session.terminal_id,
                    result.stderr.decode("utf-8", errors="replace"),
                )
        except Exception:
            LOGGER.warning("Failed to copy remote log for %s", session.terminal_id, exc_info=True)
        try:
            rm_cmd = [
                *session.ssh_prefix,
                f"rm -f {self._remote_path_arg(session.remote_log_path)}",
            ]
            subprocess.run(rm_cmd, capture_output=True, timeout=15, env=session.ssh_env)
        except Exception:
            LOGGER.debug("Failed to cleanup remote log", exc_info=True)

    @staticmethod
    def _remote_path_arg(path: str) -> str:
        if path.startswith("$HOME/"):
            return '"$HOME/' + path[len("$HOME/") :].replace('"', '\\"') + '"'
        return shlex.quote(path)

    def _archive_terminal_files(self, terminal_id: str) -> None:
        live_path = self._live_dir / f"{terminal_id}.stream.log"
        archived_path = self._archived_dir / f"{terminal_id}.log"
        if live_path.exists():
            shutil.move(str(live_path), str(archived_path))

    @staticmethod
    def _read_log_range(
        path: Path,
        *,
        tail_bytes: int | None = None,
        offset: int = 0,
    ) -> bytes:
        if not path.exists():
            return b""
        with path.open("rb") as fh:
            if tail_bytes is not None:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                start = max(0, size - tail_bytes, offset)
                fh.seek(start)
                return fh.read()
            fh.seek(offset)
            return fh.read()

    # ------------------------------------------------------------------
    # 日志轮转
    # ------------------------------------------------------------------

    def _ensure_rotation(self) -> None:
        if self._shutting_down:
            return
        if self._rotation_task is None or self._rotation_task.done():
            self._rotation_task = asyncio.create_task(
                self._rotation_loop(),
                name="interactive-terminal-log-rotation",
            )

    async def _rotation_loop(self) -> None:
        while not self._shutting_down:
            await asyncio.sleep(LOG_ROTATION_INTERVAL_SECONDS)
            try:
                async with self._lock:
                    sessions = list(self._sessions.values())
                for session in sessions:
                    if session.is_local:
                        self._rotate_local_log(session)
                    else:
                        await asyncio.to_thread(
                            self._rotate_remote_log, session
                        )
            except Exception:
                LOGGER.debug("Log rotation error", exc_info=True)

    def _rotate_local_log(self, session: InteractiveSession) -> None:
        if not session.log_path.exists():
            return
        size = session.log_path.stat().st_size
        if size <= self.max_log_mb * 1024 * 1024:
            return
        keep_bytes = self.max_log_mb * 1024 * 1024 // 2
        try:
            with session.log_path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                total = fh.tell()
                fh.seek(max(0, total - keep_bytes))
                tail = fh.read()
            with session.log_path.open("wb") as fh:
                fh.write(tail)
        except OSError:
            LOGGER.debug("Failed to rotate local log %s", session.log_path, exc_info=True)

    def _rotate_remote_log(self, session: InteractiveSession) -> None:
        if session.remote_log_path is None or session.ssh_prefix is None:
            return
        max_bytes = self.remote_max_log_mb * 1024 * 1024
        keep_bytes = max_bytes // 2
        remote_log_path = self._remote_path_arg(session.remote_log_path)
        check_cmd = [
            *session.ssh_prefix,
            f"stat -c %s {remote_log_path} 2>/dev/null || echo 0",
        ]
        try:
            result = subprocess.run(
                check_cmd,
                capture_output=True,
                text=True,
                env=session.ssh_env,
                timeout=15,
            )
            size = int(result.stdout.strip() or "0")
        except Exception:
            return
        if size <= max_bytes:
            return
        rotate_cmd = [
            *session.ssh_prefix,
            f"tail -c {keep_bytes} {remote_log_path} > {remote_log_path}.tmp && "
            f"mv {remote_log_path}.tmp {remote_log_path}",
        ]
        try:
            subprocess.run(
                rotate_cmd,
                capture_output=True,
                env=session.ssh_env,
                timeout=30,
            )
        except Exception:
            LOGGER.debug("Failed to rotate remote log", exc_info=True)

    # ------------------------------------------------------------------
    # 孤儿清理
    # ------------------------------------------------------------------

    def _cleanup_orphaned_tmux_sessions(
        self, active_terminals: list[dict[str, object]]
    ) -> None:
        known_sessions = {
            str(t["tmux_session"]) for t in active_terminals
        }
        try:
            all_sessions = tmux_list_sessions(self.tmux_socket)
        except Exception:
            return
        for session_name in all_sessions:
            if session_name.startswith(TMUX_SESSION_PREFIX) and session_name not in known_sessions:
                try:
                    tmux_kill_session(self.tmux_socket, session_name)
                except Exception:
                    LOGGER.debug("Failed to kill orphaned tmux session %s", session_name, exc_info=True)

    # ------------------------------------------------------------------
    # SSH 辅助
    # ------------------------------------------------------------------

    def _build_ssh_prefix(
        self, auth: ResolvedAuth
    ) -> tuple[list[str], dict[str, str]]:
        argv, env_extra = build_ssh_command(
            auth,
            known_hosts_path=self.known_hosts_path or "",
            extra_options=["-tt"],
            ssh_binary=self.ssh_binary,
            sshpass_binary=self.sshpass_binary,
        )
        env = os.environ.copy()
        env.update(env_extra)
        return argv, env

    def _build_ssh_prefix_from_stored(
        self, terminal: dict[str, object]
    ) -> tuple[list[str], dict[str, str]] | tuple[None, None]:
        try:
            auth = self.node_resolver(str(terminal["node_id"]))
            if auth.is_local:
                return None, None
            return self._build_ssh_prefix(auth)
        except Exception:
            return None, None

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _get_session(self, session_id: str) -> InteractiveSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"终端会话 {session_id} 不存在")
        return session

    def _session_info(self, session: InteractiveSession) -> dict[str, object]:
        return {
            "session_id": session.terminal_id,
            "name": session.name,
            "node_id": session.node_id,
            "node_name": session.node_name,
            "is_local": session.is_local,
            "created_at": _isoformat(session.created_at),
            "last_activity_at": _isoformat(session.last_activity_at),
            "alive": not session.closed,
            "exit_code": session.exit_code,
            "subscriber_count": len(session.subscribers),
            "cols": session.cols,
            "rows": session.rows,
        }


_PIDFD_OPEN_SYSCALL = 434


def _pidfd_open(pid: int) -> int:
    opener = getattr(os, "pidfd_open", None)
    if opener is not None:
        return int(opener(pid))
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(
        ctypes.c_long(_PIDFD_OPEN_SYSCALL),
        ctypes.c_int(pid),
        ctypes.c_uint(0),
    )
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(fd)


def _isoformat(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()
