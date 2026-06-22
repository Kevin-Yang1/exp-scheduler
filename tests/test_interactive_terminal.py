from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import struct
import termios
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn

from exp_scheduler_app.conda_inventory import CondaInventoryService
from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.events import EventBroker
from exp_scheduler_app.interactive_terminal import InteractiveTerminalService
from exp_scheduler_app.nodes import ResolvedAuth
from exp_scheduler_app.web import create_app


LOCAL_AUTH = ResolvedAuth(
    node_id="local",
    name="本机",
    is_local=True,
    host=None,
    port=0,
    username=None,
    auth_method=None,
    key_path=None,
    password=None,
)


def local_resolver(node_id: str) -> ResolvedAuth:
    if node_id != "local":
        raise ValueError(f"节点不存在: {node_id}")
    return LOCAL_AUTH


def make_builder(argv: list[str]):
    """返回固定 argv 的 command_builder（不依赖 ssh/登录 shell）。"""

    def builder(auth: ResolvedAuth, *, known_hosts_path=None):
        return list(argv), dict(os.environ)

    return builder


cat_builder = make_builder(["cat"])


def make_service(
    tmp_path,
    *,
    command_builder=None,
    max_sessions: int = 16,
    idle_timeout_seconds: float = 1800.0,
) -> InteractiveTerminalService:
    return InteractiveTerminalService(
        state_dir=tmp_path / "interactive-terminals",
        events=EventBroker(),
        node_resolver=local_resolver,
        max_sessions=max_sessions,
        idle_timeout_seconds=idle_timeout_seconds,
        command_builder=command_builder or cat_builder,
    )


# ---------------------------------------------------------------------------
# service 层：真实 PTY
# ---------------------------------------------------------------------------


def test_service_echo_resize_and_close(tmp_path):
    async def scenario():
        svc = make_service(tmp_path)
        try:
            info = await svc.create_session("local", cols=120, rows=30)
            session_id = str(info["session_id"])
            assert info["node_id"] == "local"
            assert info["is_local"] is True
            assert info["alive"] is True

            subscriber, snapshot, sub_info = await svc.subscribe(session_id)
            assert sub_info["session_id"] == session_id
            assert "连接" in snapshot.decode("utf-8", errors="replace")

            await svc.write_input(session_id, b"hello\n")
            collected = bytearray()
            deadline = time.time() + 5
            while b"hello" not in collected:
                remaining = deadline - time.time()
                assert remaining > 0, f"5 秒内未收到 PTY 回显: {bytes(collected)!r}"
                chunk = await asyncio.wait_for(
                    subscriber.chunk_queue.get(), timeout=remaining
                )
                collected += chunk

            # resize 后用 TIOCGWINSZ 从 PTY 读回窗口大小
            await svc.resize(session_id, cols=100, rows=40)
            master_fd = svc._sessions[session_id].terminal.master_fd
            packed = fcntl.ioctl(
                master_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
            )
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            assert (cols, rows) == (100, 40)

            process = svc._sessions[session_id].process
            await svc.unsubscribe(session_id, subscriber)
            await svc.close_session(session_id)
            assert process.poll() is not None
            assert await svc.list_sessions() == []
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_service_rejects_sessions_over_limit(tmp_path):
    async def scenario():
        svc = make_service(tmp_path, max_sessions=1)
        try:
            await svc.create_session("local")
            with pytest.raises(ValueError, match="上限"):
                await svc.create_session("local")
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_service_reaps_idle_session_without_subscribers(tmp_path):
    async def scenario():
        svc = make_service(tmp_path, idle_timeout_seconds=0.2)
        try:
            await svc.create_session("local")
            assert len(await svc.list_sessions()) == 1
            deadline = time.time() + 5
            while await svc.list_sessions():
                assert time.time() < deadline, "闲置会话未被 reaper 自动回收"
                await asyncio.sleep(0.05)
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 退出路径
# ---------------------------------------------------------------------------


def test_service_exit_payload_reports_exit_code(tmp_path):
    async def scenario():
        svc = make_service(
            tmp_path,
            command_builder=make_builder(["bash", "-c", "sleep 0.3; exit 7"]),
        )
        try:
            info = await svc.create_session("local")
            subscriber, _snapshot, _info = await svc.subscribe(str(info["session_id"]))
            event_type, payload = await asyncio.wait_for(
                subscriber.control_queue.get(), timeout=5
            )
            assert event_type == "exit"
            assert payload is not None
            assert payload["exit_code"] == 7
            assert payload["reason"] == "exited"
            assert payload["status"] == "failed"
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_service_exit_255_marks_connection_lost(tmp_path):
    async def scenario():
        svc = make_service(
            tmp_path,
            command_builder=make_builder(["bash", "-c", "sleep 0.3; exit 255"]),
        )
        try:
            info = await svc.create_session("local")
            subscriber, _snapshot, _info = await svc.subscribe(str(info["session_id"]))
            event_type, payload = await asyncio.wait_for(
                subscriber.control_queue.get(), timeout=5
            )
            assert event_type == "exit"
            assert payload is not None
            assert payload["exit_code"] == 255
            assert payload["reason"] == "connection_lost"
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_orphan_background_process_does_not_hang_close_or_shutdown(tmp_path):
    """回归：子进程遗留后台进程持有 PTY slave fd（EIO 永不到来）时，
    watch 应在数秒内观察到退出，close_session / shutdown 不得挂死。

    trap '' HUP 让后台 sleep 免于 session leader 退出时的 SIGHUP，
    持续以 stdout/stderr 持有 slave fd，复现 reader 永远读不到 EIO 的场景。
    shell 通过 ready 标记文件等待 trap 安装完成后才退出，避免 SIGHUP
    在 trap 生效前送达的竞态。
    """
    ready_marker = tmp_path / "orphan-ready"
    orphan_builder = make_builder(
        [
            "bash",
            "-c",
            f"( trap '' HUP; : > '{ready_marker}'; exec sleep 300 ) &\n"
            f"while [ ! -e '{ready_marker}' ]; do sleep 0.01; done\n"
            "exit 0",
        ]
    )

    async def scenario():
        svc = make_service(tmp_path, command_builder=orphan_builder)
        process = None
        try:
            info = await svc.create_session("local")
            session_id = str(info["session_id"])
            process = svc._sessions[session_id].process
            subscriber, _snapshot, _info = await svc.subscribe(session_id)

            # watch 应在数秒内观察到 shell 退出并发布 exit 事件（旧实现中
            # reader 永久阻塞在 os.read，watch 卡在 gather(reader_task)）
            event_type, payload = await asyncio.wait_for(
                subscriber.control_queue.get(), timeout=5
            )
            assert event_type == "exit"
            assert payload is not None
            assert payload["exit_code"] == 0
            assert payload["reason"] == "exited"

            # 此刻遗留的后台 sleep 仍存活（进程组仍有成员），证明 slave fd
            # 确实被持有、EIO 不会到来——会话收尾走的是超时路径
            os.killpg(process.pid, 0)

            # close_session 必须在 2s 内返回（会话可能已被 watch 收尾弹出）
            try:
                await asyncio.wait_for(svc.close_session(session_id), timeout=2)
            except ValueError as exc:
                assert "不存在" in str(exc)

            deadline = time.time() + 2
            while await svc.list_sessions():
                assert time.time() < deadline, "会话未在 2 秒内从列表移除"
                await asyncio.sleep(0.05)
        finally:
            # shutdown 不得挂死
            await asyncio.wait_for(svc.shutdown(), timeout=5)
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)

    asyncio.run(scenario())


def test_write_input_raises_when_pty_buffer_full(tmp_path):
    """回归：master 改非阻塞后，子进程不消费 stdin 导致 PTY 输入缓冲打满时，
    write_input 应在限时重试后抛"终端输入缓冲区已满"，而非占用线程永久阻塞写。"""

    async def scenario():
        svc = make_service(tmp_path, command_builder=make_builder(["sleep", "30"]))
        try:
            info = await svc.create_session("local")
            session_id = str(info["session_id"])

            # 关闭 ICANON/ECHO（raw 模式）：canonical 模式下行缓冲溢出会被
            # 行规程丢弃而非阻塞，raw 模式下输入队列打满才会真正 EAGAIN
            master_fd = svc._sessions[session_id].terminal.master_fd
            attrs = termios.tcgetattr(master_fd)
            attrs[3] &= ~(termios.ICANON | termios.ECHO)
            termios.tcsetattr(master_fd, termios.TCSANOW, attrs)

            chunk = b"x" * 8192

            async def write_until_full():
                # PTY 输入队列约 4-16KB，128KB 必然触发缓冲打满
                for _ in range(16):
                    await svc.write_input(session_id, chunk)

            with pytest.raises(ValueError, match="终端输入缓冲区已满"):
                await asyncio.wait_for(write_until_full(), timeout=10)
        finally:
            await asyncio.wait_for(svc.shutdown(), timeout=15)

    asyncio.run(scenario())


def test_sessions_do_not_consume_executor_threads(tmp_path):
    """回归：reader 改 loop.add_reader、process.wait 改 pidfd 等待后，
    会话存续期间不再占用线程池常驻线程（旧实现每会话长期占 2 个线程）。"""

    async def scenario():
        svc = make_service(tmp_path)
        try:
            baseline = threading.active_count()
            for _ in range(3):
                await svc.create_session("local")
            await asyncio.sleep(0.3)
            assert threading.active_count() <= baseline, (
                "创建交互终端会话不应派生常驻阻塞线程"
            )
        finally:
            await asyncio.wait_for(svc.shutdown(), timeout=15)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def make_config(tmp_path, *, port: int = 17861, state_name: str = "state") -> SchedulerConfig:
    return SchedulerConfig(
        host="127.0.0.1",
        port=port,
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        gpu_idle_required_checks=1,
        state_dir=tmp_path / state_name,
        log_dir=tmp_path / state_name / "logs",
    )


def start_live_server(tmp_path, *, command_builder):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = make_config(tmp_path, port=port, state_name="state-live")
    app = create_app(
        config,
        gpu_provider=lambda: [],
        interactive_command_builder=command_builder,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server, thread, port


def test_api_terminal_lifecycle_with_sse_stream(tmp_path):
    server, thread, port = start_live_server(tmp_path, command_builder=cat_builder)
    base_url = f"http://127.0.0.1:{port}"
    try:
        with (
            httpx.Client(timeout=10.0, trust_env=False) as control,
            httpx.Client(timeout=10.0, trust_env=False) as stream_client,
        ):
            created = control.post(f"{base_url}/api/terminals", json={"node_id": "local"})
            created.raise_for_status()
            session = created.json()["session"]
            session_id = session["session_id"]
            assert session["node_id"] == "local"
            assert session["alive"] is True

            listed = control.get(f"{base_url}/api/terminals").json()["sessions"]
            assert [item["session_id"] for item in listed] == [session_id]

            got_snapshot = False
            collected = bytearray()
            event_name: str | None = None
            deadline = time.time() + 10
            with stream_client.stream(
                "GET", f"{base_url}/api/terminals/{session_id}/stream"
            ) as stream:
                for line in stream.iter_lines():
                    assert time.time() < deadline, "SSE 流 10 秒内未收到回显"
                    if line.startswith("event: "):
                        event_name = line[len("event: "):]
                        continue
                    if not line.startswith("data: "):
                        continue
                    payload = json.loads(line[len("data: "):])
                    if event_name == "snapshot":
                        got_snapshot = True
                        # 收到 snapshot 后再写输入，保证回显走 chunk 事件
                        data = base64.b64encode(b"hello\n").decode("ascii")
                        resp = control.post(
                            f"{base_url}/api/terminals/{session_id}/input",
                            json={"data": data},
                        )
                        assert resp.status_code == 200
                    elif event_name == "chunk":
                        collected += base64.b64decode(payload["data"])
                        if b"hello" in collected:
                            break
            assert got_snapshot
            assert b"hello" in collected

            resize = control.post(
                f"{base_url}/api/terminals/{session_id}/resize",
                json={"cols": 90, "rows": 36},
            )
            assert resize.status_code == 200

            closed = control.delete(f"{base_url}/api/terminals/{session_id}")
            assert closed.status_code == 200
            assert control.get(f"{base_url}/api/terminals").json()["sessions"] == []
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_api_input_validation_and_missing_resources(tmp_path):
    config = make_config(tmp_path)
    app = create_app(
        config,
        gpu_provider=lambda: [],
        interactive_command_builder=cat_builder,
    )
    # 用上下文管理器跑 lifespan（database.init 在 scheduler.startup 中执行）
    with TestClient(app) as client:
        # 未注册的 node_id（resolve_auth 抛"节点不存在"）→ 404
        resp = client.post("/api/terminals", json={"node_id": "ghost"})
        assert resp.status_code == 404

        # 不存在的会话写输入 → 404
        valid = base64.b64encode(b"ls\n").decode("ascii")
        resp = client.post("/api/terminals/no-such/input", json={"data": valid})
        assert resp.status_code == 404

        # 非法 base64 → 400
        resp = client.post("/api/terminals/no-such/input", json={"data": "!!!"})
        assert resp.status_code == 400

        # 超过 64KB → 400（大小校验先于会话查找）
        oversized = base64.b64encode(b"x" * (64 * 1024 + 1)).decode("ascii")
        resp = client.post("/api/terminals/no-such/input", json={"data": oversized})
        assert resp.status_code == 400
        assert "64KB" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# conda inventory
# ---------------------------------------------------------------------------


class FakeNodesRegistry:
    """CondaInventoryService 只用到 list_nodes / resolve_auth / known_hosts_path。"""

    def __init__(self, nodes: list[dict[str, object]]) -> None:
        self._nodes = nodes

    async def list_nodes(self) -> list[dict[str, object]]:
        return [dict(node) for node in self._nodes]

    def resolve_auth(self, node_id: str) -> ResolvedAuth:
        if node_id == "local":
            return LOCAL_AUTH
        return ResolvedAuth(
            node_id=node_id,
            name=f"node-{node_id}",
            is_local=False,
            host="198.51.100.10",
            port=22,
            username="ubuntu",
            auth_method="key",
            key_path="/tmp/fake-key",
            password=None,
        )

    def known_hosts_path(self) -> Path:
        return Path("/tmp/fake-known-hosts")


def test_conda_inventory_parses_remote_envs(tmp_path):
    async def scenario():
        nodes = FakeNodesRegistry(
            [
                {"id": "local", "name": "本机", "is_local": True},
                {"id": "n1", "name": "worker-1", "is_local": False},
            ]
        )
        seen_commands: list[tuple[str, str]] = []

        async def runner(auth: ResolvedAuth, command: str):
            seen_commands.append((auth.node_id, command))
            return (0, '{"envs":["/opt/conda","/opt/conda/envs/test"]}', "")

        def fake_local():
            return (
                [{"display_name": "base"}, {"display_name": "ml"}],
                Path("/no/such/conda"),
            )

        svc = CondaInventoryService(
            nodes=nodes,
            profile_discovery_provider=fake_local,
            runner=runner,
        )
        inventory = await svc.get_inventory(refresh=True)
        by_id = {entry["node_id"]: entry for entry in inventory["nodes"]}

        remote = by_id["n1"]
        assert remote["status"] == "ok"
        assert remote["envs"] == ["base", "test"]
        assert remote["error"] is None

        # 远程探测只对非本地节点发起，且走登录 shell
        assert [node_id for node_id, _ in seen_commands] == ["n1"]
        assert "conda env list --json" in seen_commands[0][1]

        local = by_id["local"]
        assert local["status"] == "ok"
        assert local["envs"] == ["base", "ml"]

    asyncio.run(scenario())


def test_conda_inventory_marks_timeout_node(tmp_path):
    async def scenario():
        nodes = FakeNodesRegistry([{"id": "n1", "name": "worker-1", "is_local": False}])

        async def runner(auth: ResolvedAuth, command: str):
            raise TimeoutError

        svc = CondaInventoryService(nodes=nodes, runner=runner)
        inventory = await svc.get_inventory(refresh=True)
        entry = inventory["nodes"][0]
        assert entry["status"] == "timeout"
        assert entry["envs"] == []
        assert "超时" in str(entry["error"])

    asyncio.run(scenario())


def test_conda_inventory_uses_injected_local_provider(tmp_path):
    async def scenario():
        nodes = FakeNodesRegistry([{"id": "local", "name": "本机", "is_local": True}])

        def fake_local():
            return (
                [{"display_name": "base"}, {"display_name": "torch"}],
                Path("/no/such/conda"),
            )

        svc = CondaInventoryService(nodes=nodes, profile_discovery_provider=fake_local)
        inventory = await svc.get_inventory(refresh=True)
        entry = inventory["nodes"][0]
        assert entry["status"] == "ok"
        assert entry["envs"] == ["base", "torch"]

    asyncio.run(scenario())
