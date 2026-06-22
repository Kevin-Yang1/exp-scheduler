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
import signal
import subprocess
import time
import traceback
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from .nodes import ResolvedAuth


LOGGER = logging.getLogger("exp_scheduler")

# 关闭梯度每级等待秒数：SIGHUP → 3s → SIGTERM → 3s → SIGKILL
INTERACTIVE_TERMINATE_GRACE_SECONDS = 3
# 闲置会话回收扫描周期
REAPER_INTERVAL_SECONDS = 60.0
# 单次输入字节上限（防御性限制，web 层也应校验）
MAX_INPUT_BYTES = 64 * 1024
# 进程退出后等待 reader 收到 EIO（排空残余输出）的宽限秒数：
# 子进程遗留后台进程仍持有 slave fd 时 EIO 永不到来，超时后直接收尾
READER_DRAIN_GRACE_SECONDS = 1.0
# 非阻塞写输入的总超时与重试间隔（PTY 输入缓冲满时小步重试剩余字节）
INPUT_WRITE_TIMEOUT_SECONDS = 2.0
INPUT_WRITE_RETRY_INTERVAL_SECONDS = 0.05


def build_terminal_command(
    auth: ResolvedAuth,
    *,
    known_hosts_path: Path | None = None,
) -> tuple[list[str], dict[str, str]]:
    """构造交互终端启动命令（纯函数，可注入替换供测试）。

    本地节点启动登录 shell（保证 conda init 生效）；远程节点用 ssh -tt
    打开登录 shell（不追加远程命令）。返回 (argv, env)。
    """
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    if auth.is_local:
        shell = os.environ.get("SHELL") or "/bin/bash"
        return [shell, "-l"], env

    if not auth.host or not auth.username:
        raise ValueError(f"节点 {auth.name} 缺少主机或用户名配置")

    argv: list[str] = ["ssh", "-tt", "-p", str(auth.port)]
    argv += ["-o", "StrictHostKeyChecking=accept-new"]
    if known_hosts_path is not None:
        argv += ["-o", f"UserKnownHostsFile={known_hosts_path}"]
    argv += [
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=10",
        "-o", "NumberOfPasswordPrompts=1",
    ]

    if auth.auth_method == "password":
        if not auth.password:
            raise ValueError(f"节点 {auth.name} 未配置密码")
        if shutil.which("sshpass") is None:
            raise ValueError("sshpass 未安装，无法连接密码认证节点，请先安装 sshpass")
        argv += [
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
        ]
        argv = ["sshpass", "-e", *argv]
        # 密码只经环境变量传递，绝不进入 argv / 日志
        env["SSHPASS"] = auth.password
    else:
        if not auth.key_path:
            raise ValueError(f"节点 {auth.name} 未配置 SSH 密钥")
        argv += ["-i", auth.key_path, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]

    argv.append(f"{auth.username}@{auth.host}")
    return argv, env


@dataclass(slots=True, eq=False)
class InteractiveSession:
    session_id: str
    node_id: str
    node_name: str
    is_local: bool
    terminal: TerminalSession
    process: subprocess.Popen[bytes]
    watch_task: asyncio.Task[None] | None
    created_at: float
    last_activity_at: float
    exit_code: int | None = None
    exit_reason: str | None = None
    # reader 收到 EIO/EOF（输出结束）时置位；_watch_process 限时等待它排空残余输出
    reader_done: asyncio.Event = field(default_factory=asyncio.Event)


class InteractiveTerminalService:
    """多节点交互终端服务：每个会话一个 PTY（本地登录 shell 或 ssh -tt）。

    生命周期约定：SSE 断开仅 unsubscribe，会话保留（shell 可能正在跑
    conda install）；reaper 周期回收"无订阅且超时无活动"的会话。
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        events,
        node_resolver: Callable[[str], ResolvedAuth],
        max_sessions: int = 16,
        idle_timeout_seconds: float = 1800.0,
        command_builder: Callable[..., tuple[list[str], dict[str, str]]] | None = None,
        known_hosts_path: Path | None = None,
        database=None,
    ) -> None:
        self.state_dir = state_dir
        self.events = events
        self.node_resolver = node_resolver
        self.max_sessions = max_sessions
        self.idle_timeout_seconds = idle_timeout_seconds
        self.command_builder = command_builder or build_terminal_command
        self.known_hosts_path = known_hosts_path
        self.database = database
        self._sessions: dict[str, InteractiveSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        # 服务重启后清扫上一次遗留的会话日志（会话不跨重启持久化）
        self._cleanup_stale_logs()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def create_session(
        self,
        node_id: str,
        cols: int | None = None,
        rows: int | None = None,
    ) -> dict[str, object]:
        if self._shutting_down:
            raise ValueError("终端服务正在关闭，无法创建新会话")
        auth = self.node_resolver(node_id)
        argv, env = self.command_builder(auth, known_hosts_path=self.known_hosts_path)
        async with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise ValueError(
                    f"交互终端数已达上限（{self.max_sessions}），请先关闭闲置会话"
                )
            session = self._start_session_locked(
                auth=auth,
                argv=argv,
                env=env,
                cols=cols or DEFAULT_TERMINAL_COLUMNS,
                rows=rows or DEFAULT_TERMINAL_ROWS,
            )
            self._sessions[session.session_id] = session
        self._ensure_reaper()
        info = self._session_info(session)
        await self._record_operation(
            level="info",
            action="terminal_session_created",
            title=f"创建交互终端: {session.node_name}",
            entity_id=None,
            metadata={
                "session_id": session.session_id,
                "node_id": session.node_id,
                "node_name": session.node_name,
                "is_local": session.is_local,
            },
        )
        await self.events.publish(
            "terminal_session_created",
            {
                "session_id": session.session_id,
                "node_id": session.node_id,
                "node_name": session.node_name,
            },
        )
        return info

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
        session = self._get_session(session_id)
        if session.terminal.closed:
            raise ValueError(f"终端会话 {session_id} 已退出")
        # 首个订阅者可设定终端尺寸（后续订阅不抢占已有窗口大小）
        if cols is not None and rows is not None and not session.terminal.subscribers:
            try:
                session.terminal.resize(cols=cols, rows=rows)
            except OSError:
                LOGGER.debug("Failed to resize interactive terminal", exc_info=True)
        subscriber, snapshot = session.terminal.subscribe(
            snapshot_bytes=TERMINAL_SNAPSHOT_BYTES
        )
        session.last_activity_at = time.time()
        return subscriber, snapshot, self._session_info(session)

    async def unsubscribe(self, session_id: str, subscriber: TerminalSubscriber) -> None:
        # 仅移除订阅者，不关会话（shell 可能正在跑 conda install）
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.terminal.unsubscribe(subscriber)
        session.last_activity_at = time.time()

    async def write_input(self, session_id: str, data: bytes) -> None:
        session = self._get_session(session_id)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError(f"单次输入超过 {MAX_INPUT_BYTES // 1024}KB 上限")
        if session.terminal.closed or session.process.poll() is not None:
            raise ValueError(f"终端会话 {session_id} 已退出")
        if data:
            await self._write_input_bytes(session, data)
        session.last_activity_at = time.time()

    async def _write_input_bytes(self, session: InteractiveSession, data: bytes) -> None:
        """非阻塞写输入到 PTY master（在事件循环上执行）。

        master 为非阻塞模式：持锁校验会话未关闭后直接 os.write，校验与
        写入之间无让出点，且关闭也在事件循环执行，消除 fd 捕获后被并发
        close/复用的竞态。缓冲满（BlockingIOError/部分写）时小步重试剩余
        字节，超时仍写不进则报错。
        """
        view = memoryview(data)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INPUT_WRITE_TIMEOUT_SECONDS
        while True:
            async with self._lock:
                terminal = session.terminal
                if terminal.closed or terminal.master_fd < 0:
                    raise ValueError(f"终端会话 {session.session_id} 已退出")
                try:
                    written = os.write(terminal.master_fd, view)
                except BlockingIOError:
                    written = 0
                except OSError as exc:
                    raise ValueError(
                        f"终端会话 {session.session_id} 已退出"
                    ) from exc
            view = view[written:]
            if not view:
                return
            if loop.time() >= deadline:
                raise ValueError("终端输入缓冲区已满")
            await asyncio.sleep(INPUT_WRITE_RETRY_INTERVAL_SECONDS)

    async def resize(self, session_id: str, *, cols: int, rows: int) -> None:
        session = self._get_session(session_id)
        if session.terminal.closed or session.process.poll() is not None:
            raise ValueError(f"终端会话 {session_id} 已退出")
        try:
            session.terminal.resize(cols=cols, rows=rows)
        except OSError as exc:
            raise ValueError(f"终端会话 {session_id} 已退出") from exc
        session.last_activity_at = time.time()

    async def close_session(self, session_id: str) -> None:
        session = self._get_session(session_id)
        await self._close_session_object(session)

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            await asyncio.gather(self._reaper_task, return_exceptions=True)
            self._reaper_task = None
        async with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                await self._close_session_object(session)
            except Exception:
                LOGGER.warning(
                    "Failed to close interactive terminal %s during shutdown",
                    session.session_id,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _get_session(self, session_id: str) -> InteractiveSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError(f"终端会话 {session_id} 不存在")
        return session

    def _start_session_locked(
        self,
        *,
        auth: ResolvedAuth,
        argv: list[str],
        env: dict[str, str],
        cols: int,
        rows: int,
    ) -> InteractiveSession:
        session_id = secrets.token_urlsafe(8)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.state_dir / f"{session_id}.log"
        log_file = open(log_path, "w+b")
        os.chmod(log_path, 0o600)
        master_fd, slave_fd = pty.openpty()
        # master 非阻塞：读取走 loop.add_reader，写输入在事件循环直接 os.write
        os.set_blocking(master_fd, False)
        terminal_cols, terminal_rows = set_terminal_window_size(
            slave_fd,
            cols=cols,
            rows=rows,
        )
        terminal = TerminalSession(
            task_id=0,
            master_fd=master_fd,
            log_path=log_path,
            log_file=log_file,
            cols=terminal_cols,
            rows=terminal_rows,
        )
        terminal.append_bytes(
            encode_terminal_text(f"[exp-scheduler] 连接 {auth.name} ...\n")
        )

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
        except Exception as exc:
            terminal.append_bytes(encode_terminal_text("[exp-scheduler] 终端启动失败\n"))
            terminal.append_bytes(
                encode_terminal_text("".join(traceback.format_exception(exc)))
            )
            self._close_master_fd(terminal)
            log_file.close()
            try:
                os.close(slave_fd)
            except OSError:
                pass
            raise ValueError(f"终端启动失败: {exc}") from exc
        finally:
            try:
                os.close(slave_fd)
            except OSError:
                pass

        now = time.time()
        session = InteractiveSession(
            session_id=session_id,
            node_id=auth.node_id,
            node_name=auth.name,
            is_local=auth.is_local,
            terminal=terminal,
            process=process,
            watch_task=None,
            created_at=now,
            last_activity_at=now,
        )
        # 读取不再用常驻线程：master 已非阻塞，注册到事件循环的 reader
        asyncio.get_running_loop().add_reader(
            master_fd, self._on_master_readable, session
        )
        session.watch_task = asyncio.create_task(
            self._watch_process(session),
            name=f"interactive-terminal-watch-{session_id}",
        )
        return session

    def _on_master_readable(self, session: InteractiveSession) -> None:
        """master fd 可读回调（事件循环线程）：循环非阻塞读直到 EAGAIN。

        append_bytes（写日志 + 分发订阅者队列）是轻量同步操作，直接调用。
        EIO/EBADF/EOF 表示所有 slave 持有者退出或 fd 已关闭：注销 reader
        并标记输出结束。
        """
        terminal = session.terminal
        if terminal.closed or terminal.master_fd < 0:
            self._detach_reader(session)
            return
        while True:
            try:
                data = os.read(terminal.master_fd, TERMINAL_CHUNK_BYTES)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno not in {errno.EIO, errno.EBADF}:
                    LOGGER.warning("Failed to read interactive PTY output: %s", exc)
                break
            if not data:
                break
            session.last_activity_at = time.time()
            terminal.append_bytes(data)
        self._detach_reader(session)

    def _detach_reader(self, session: InteractiveSession) -> None:
        """注销 master fd 的事件循环 reader 并标记输出结束。"""
        terminal = session.terminal
        if terminal.master_fd >= 0:
            with contextlib.suppress(RuntimeError, ValueError, OSError):
                asyncio.get_running_loop().remove_reader(terminal.master_fd)
        session.reader_done.set()

    async def _wait_process(self, process: subprocess.Popen[bytes]) -> int:
        """等待子进程退出：pidfd + add_reader，不占用线程池常驻线程。

        pidfd 为 Linux 专属（本项目 Linux-only）；打开失败时
        （如进程已被回收）回退到线程等待。
        """
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
        # 进程已退出，wait 仅做收尸，立即返回
        return process.wait()

    async def _watch_process(self, session: InteractiveSession) -> None:
        exit_payload: dict[str, object] | None = None
        try:
            exit_code = await self._wait_process(session.process)
            session.exit_code = exit_code
            # ssh 连接断开（ServerAlive 超时/网络中断）退出码为 255
            reason = "connection_lost" if exit_code == 255 else "exited"
            session.exit_reason = reason
            # 限时等待 reader 排空残余输出（收到 EIO）。子进程遗留的后台进程
            # 仍持有 slave fd 时 EIO 永不到来，超时后直接收尾，避免会话挂死
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    session.reader_done.wait(),
                    timeout=READER_DRAIN_GRACE_SECONDS,
                )
            status = "succeeded" if exit_code == 0 else "failed"
            if not session.terminal.closed:
                if reason == "connection_lost":
                    note = (
                        f"\n[exp-scheduler] 与 {session.node_name} 的连接已断开 "
                        f"(exit_code={exit_code})\n"
                    )
                else:
                    note = f"\n[exp-scheduler] 会话已结束 exit_code={exit_code}\n"
                session.terminal.append_bytes(encode_terminal_text(note))
            exit_payload = {
                "session_id": session.session_id,
                "node_id": session.node_id,
                "node_name": session.node_name,
                "status": status,
                "exit_code": exit_code,
                "reason": reason,
            }
        finally:
            self._finalize_session(session, exit_payload)
            async with self._lock:
                self._sessions.pop(session.session_id, None)
            if exit_payload is not None:
                await self.events.publish("terminal_session_closed", exit_payload)
                level = "info" if session.exit_code == 0 else "warning"
                await self._record_operation(
                    level=level,
                    action="terminal_session_closed",
                    title=f"交互终端已关闭: {session.node_name}",
                    metadata=dict(exit_payload),
                )

    def _finalize_session(
        self,
        session: InteractiveSession,
        exit_payload: dict[str, object] | None,
    ) -> None:
        terminal = session.terminal
        if exit_payload is not None and not terminal.closed:
            terminal.publish_exit(exit_payload)
        elif exit_payload is None and not terminal.closed:
            terminal.closed = True
            for subscriber in list(terminal.subscribers):
                subscriber.control_queue.put_nowait(("disconnect", None))
            terminal.subscribers.clear()
        self._close_master_fd(terminal)
        if not terminal.log_file.closed:
            terminal.log_file.flush()
            terminal.log_file.close()

    async def _close_session_object(self, session: InteractiveSession) -> None:
        if session.process.poll() is None:
            await self._terminate_process(session.process)
        if session.watch_task is not None and session.watch_task is not asyncio.current_task():
            await asyncio.gather(session.watch_task, return_exceptions=True)

    async def _terminate_process(self, process: subprocess.Popen[bytes]) -> None:
        # 关闭梯度：SIGHUP → 3s → SIGTERM → 3s → SIGKILL（killpg 整个进程组）
        for sig in (signal.SIGHUP, signal.SIGTERM):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(
                    self._wait_process(process),
                    timeout=INTERACTIVE_TERMINATE_GRACE_SECONDS,
                )
                return
            except asyncio.TimeoutError:
                continue
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await self._wait_process(process)

    def _close_master_fd(self, terminal: TerminalSession) -> None:
        if terminal.master_fd < 0:
            return
        # 先从事件循环注销 reader 再关闭 fd，避免已关闭/被复用的 fd 继续触发回调
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            with contextlib.suppress(RuntimeError, ValueError, OSError):
                loop.remove_reader(terminal.master_fd)
        try:
            os.close(terminal.master_fd)
        except OSError:
            pass
        terminal.master_fd = -1

    # ------------------------------------------------------------------
    # 闲置回收
    # ------------------------------------------------------------------

    def _ensure_reaper(self) -> None:
        if self._shutting_down:
            return
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(
                self._reaper_loop(),
                name="interactive-terminal-reaper",
            )

    async def _reaper_loop(self) -> None:
        # idle_timeout 极小时（测试场景）按比例缩短扫描间隔
        interval = min(REAPER_INTERVAL_SECONDS, max(self.idle_timeout_seconds, 0.05))
        while True:
            await asyncio.sleep(interval)
            now = time.time()
            async with self._lock:
                stale = [
                    session
                    for session in self._sessions.values()
                    if not session.terminal.subscribers
                    and now - session.last_activity_at > self.idle_timeout_seconds
                ]
            for session in stale:
                try:
                    await self._close_session_object(session)
                    await self._record_operation(
                        level="info",
                        action="terminal_session_reaped",
                        title=f"回收闲置交互终端: {session.node_name}",
                        metadata={
                            "session_id": session.session_id,
                            "node_id": session.node_id,
                            "node_name": session.node_name,
                            "idle_timeout_seconds": self.idle_timeout_seconds,
                        },
                    )
                except Exception:
                    LOGGER.warning(
                        "Failed to reap idle interactive terminal %s",
                        session.session_id,
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _session_info(self, session: InteractiveSession) -> dict[str, object]:
        return {
            "session_id": session.session_id,
            "node_id": session.node_id,
            "node_name": session.node_name,
            "is_local": session.is_local,
            "created_at": _isoformat(session.created_at),
            "last_activity_at": _isoformat(session.last_activity_at),
            "alive": session.process.poll() is None,
            "exit_code": session.exit_code,
            "subscriber_count": len(session.terminal.subscribers),
            "cols": session.terminal.cols,
            "rows": session.terminal.rows,
        }

    def _cleanup_stale_logs(self) -> None:
        try:
            if not self.state_dir.exists():
                return
            for path in self.state_dir.glob("*.log"):
                try:
                    path.unlink()
                except OSError:
                    pass
        except OSError:
            LOGGER.debug("Failed to clean stale interactive terminal logs", exc_info=True)

    async def _record_operation(
        self,
        *,
        level: str,
        action: str,
        title: str,
        entity_type: str | None = None,
        entity_id: int | None = None,
        detail: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if self.database is None:
            return
        try:
            log = self.database.add_operation_log(
                level=level,
                source="terminal",
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                title=title,
                detail=detail,
                metadata=metadata,
            )
        except Exception:
            LOGGER.warning("Failed to write terminal operation log: %s", action, exc_info=True)
            return
        await self.events.publish(
            "operation_log_created",
            {"log_id": log["id"], "action": action},
        )


# Linux syscall 号：pidfd_open（asm-generic，x86_64 与 aarch64 一致）
_PIDFD_OPEN_SYSCALL = 434


def _pidfd_open(pid: int) -> int:
    """打开进程的 pidfd（Linux 专属），供事件循环 add_reader 等待退出。

    旧 sysroot 构建的 CPython（如部分 conda 发行版）可能未暴露
    os.pidfd_open，此时直接调 Linux 原生 syscall。
    """
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
