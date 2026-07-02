from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn

from exp_scheduler_app.conda_inventory import CondaInventoryService
from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.database import Database
from exp_scheduler_app.events import EventBroker
from exp_scheduler_app.interactive_terminal import InteractiveTerminalService
from exp_scheduler_app.nodes import ResolvedAuth
from exp_scheduler_app.tmux_utils import (
    build_tmux_capture_pane_command,
    build_tmux_setup_command,
)
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


def make_service(
    tmp_path,
    *,
    max_sessions: int = 16,
    history_limit: int = 10000,
    database: Database | None = None,
) -> InteractiveTerminalService:
    return InteractiveTerminalService(
        state_dir=tmp_path / "state",
        terminal_log_dir=tmp_path / "terminals",
        events=EventBroker(),
        node_resolver=local_resolver,
        database=database,
        max_sessions=max_sessions,
        history_limit=history_limit,
    )


# ---------------------------------------------------------------------------
# service 层：tmux-backed
# ---------------------------------------------------------------------------


def test_service_create_subscribe_echo_close(tmp_path):
    async def scenario():
        svc = make_service(tmp_path)
        try:
            info = await svc.create_session("local", cols=120, rows=30)
            sid = str(info["session_id"])
            assert info["node_id"] == "local"
            assert info["is_local"] is True
            assert info["alive"] is True
            assert info["name"]

            subscriber, snapshot, sub_info = await svc.subscribe(sid)
            assert sub_info["session_id"] == sid

            await svc.write_input(sid, b"echo hello_world\n")
            collected = bytearray()
            deadline = time.time() + 8
            while b"hello_world" not in collected:
                remaining = deadline - time.time()
                assert remaining > 0, f"8 秒内未收到回显: {bytes(collected)!r}"
                chunk = await asyncio.wait_for(
                    subscriber.chunk_queue.get(), timeout=remaining
                )
                collected += chunk
            assert b"hello_world" in collected

            await svc.unsubscribe(sid, subscriber)
            await svc.close_session(sid)
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


def test_service_persists_after_unsubscribe(tmp_path):
    """SSE 断开后 tmux session 保留，重连可获取历史 snapshot。"""

    async def scenario():
        svc = make_service(tmp_path)
        try:
            info = await svc.create_session("local", cols=120, rows=30)
            sid = str(info["session_id"])

            sub1, _snap1, _ = await svc.subscribe(sid)
            await svc.write_input(sid, b"echo persist_test\n")
            collected = bytearray()
            deadline = time.time() + 8
            while b"persist_test" not in collected:
                remaining = deadline - time.time()
                assert remaining > 0
                chunk = await asyncio.wait_for(
                    sub1.chunk_queue.get(), timeout=remaining
                )
                collected += chunk

            await svc.unsubscribe(sid, sub1)

            await asyncio.sleep(0.5)
            sessions = await svc.list_sessions()
            assert [item["session_id"] for item in sessions] == [sid]

            sub2, snap2, _ = await svc.subscribe(sid)
            snap_text = snap2.decode("utf-8", errors="replace")
            assert "persist_test" in snap_text, (
                f"重连 snapshot 应包含历史输出: {snap_text[-300:]}"
            )
            await svc.unsubscribe(sid, sub2)
            await svc.close_session(sid)
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_service_resize_updates_tmux_attach_client(tmp_path):
    async def scenario():
        svc = make_service(tmp_path)
        try:
            info = await svc.create_session("local", cols=120, rows=30)
            sid = str(info["session_id"])
            tmux_session = svc._sessions[sid].tmux_session

            await svc.resize(sid, cols=90, rows=24)

            deadline = time.time() + 4
            observed = ""
            while time.time() < deadline:
                result = subprocess.run(
                    [
                        "tmux",
                        "-L",
                        svc.tmux_socket,
                        "list-clients",
                        "-t",
                        tmux_session,
                        "-F",
                        "#{client_width}x#{client_height}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                observed = result.stdout.strip()
                if "90x24" in observed:
                    break
                await asyncio.sleep(0.1)
            assert "90x24" in observed

            await svc.close_session(sid)
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_service_rename(tmp_path):
    async def scenario():
        db = Database(tmp_path / "test.db")
        db.init()
        svc = make_service(tmp_path, database=db)
        try:
            info = await svc.create_session("local", cols=120, rows=30)
            sid = str(info["session_id"])
            assert info["name"] != "renamed-terminal"

            renamed = await svc.rename_session(sid, "renamed-terminal")
            assert renamed["name"] == "renamed-terminal"

            sessions = await svc.list_sessions()
            assert sessions[0]["name"] == "renamed-terminal"

            terminal = db.get_terminal(sid)
            assert terminal is not None
            assert terminal["name"] == "renamed-terminal"

            await svc.close_session(sid)
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_service_rename_rejects_empty(tmp_path):
    async def scenario():
        svc = make_service(tmp_path)
        try:
            info = await svc.create_session("local")
            sid = str(info["session_id"])
            with pytest.raises(ValueError, match="不能为空"):
                await svc.rename_session(sid, "  ")
            await svc.close_session(sid)
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_service_close_archives_log(tmp_path):
    async def scenario():
        db = Database(tmp_path / "test.db")
        db.init()
        svc = make_service(tmp_path, database=db)
        try:
            info = await svc.create_session("local", cols=120, rows=30)
            sid = str(info["session_id"])

            sub, _, _ = await svc.subscribe(sid)
            await svc.write_input(sid, b"echo archive_test\n")
            collected = bytearray()
            deadline = time.time() + 8
            while b"archive_test" not in collected:
                remaining = deadline - time.time()
                assert remaining > 0
                chunk = await asyncio.wait_for(sub.chunk_queue.get(), timeout=remaining)
                collected += chunk
                if b"archive_test" in collected:
                    break
            await svc.unsubscribe(sid, sub)

            await svc.close_session(sid)

            archived = db.list_terminals(status="closed")
            assert len(archived) == 1
            assert archived[0]["id"] == sid
            assert archived[0]["status"] == "closed"

            log_data = await svc.read_archived_log(sid, tail_bytes=65536)
            assert isinstance(log_data, bytes)
            assert b"archive_test" in log_data
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_service_remote_ssh_prefix_places_tty_before_target(tmp_path):
    remote_auth = ResolvedAuth(
        node_id="n1",
        name="worker-1",
        is_local=False,
        host="192.0.2.10",
        port=2222,
        username="ubuntu",
        auth_method="key",
        key_path="/tmp/fake-key",
        password=None,
    )
    svc = InteractiveTerminalService(
        state_dir=tmp_path / "state",
        terminal_log_dir=tmp_path / "terminals",
        events=EventBroker(),
        node_resolver=lambda _node_id: remote_auth,
        known_hosts_path=tmp_path / "known_hosts",
    )

    argv, _env = svc._build_ssh_prefix(remote_auth)

    assert argv[-1] == "ubuntu@192.0.2.10"
    assert "-tt" in argv
    assert "--" in argv
    assert argv.index("-tt") < argv.index("--")


def test_service_snapshot_line_feeds_are_terminal_safe():
    raw = b"first\nsecond\r\nthird\n"
    assert (
        InteractiveTerminalService._normalize_snapshot_line_feeds(raw)
        == b"first\r\nsecond\r\nthird\r\n"
    )


def test_tmux_setup_disables_status_bar(tmp_path):
    cmd = build_tmux_setup_command(
        "socket",
        session_name="expsched_test",
        history_limit=1000,
        log_path=tmp_path / "terminal.log",
    )
    text = " ".join(cmd)
    assert "set-option -t expsched_test status off" in text


def test_tmux_snapshot_uses_plain_text_capture():
    cmd = build_tmux_capture_pane_command("socket", session_name="expsched_test")
    assert "capture-pane" in cmd
    assert "-p" in cmd
    assert "-e" not in cmd


def test_service_reconcile_on_startup(tmp_path):
    """服务重启后 reconcile 重建 attach reader，tmux session 保留。"""

    async def scenario():
        db = Database(tmp_path / "test.db")
        db.init()

        svc1 = make_service(tmp_path, database=db)
        info = await svc1.create_session("local", cols=120, rows=30)
        sid = str(info["session_id"])
        await svc1.shutdown()

        active = db.list_terminals(status="active")
        assert len(active) == 1

        svc2 = make_service(tmp_path, database=db)
        await svc2.reconcile_on_startup()
        sessions = await svc2.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == sid

        sub, snap, _ = await svc2.subscribe(sid)
        assert len(snap) >= 0
        await svc2.unsubscribe(sid, sub)

        await svc2.close_session(sid)
        await svc2.shutdown()

        svc3 = make_service(tmp_path, database=db)
        await svc3.reconcile_on_startup()
        active = db.list_terminals(status="active")
        assert len(active) == 0
        await svc3.shutdown()

    asyncio.run(scenario())


def test_service_reconcile_rejects_log_dir_change(tmp_path):
    """terminal_log_dir 变更且有记录时拒绝启动。"""

    async def scenario():
        db = Database(tmp_path / "test.db")
        db.init()

        svc1 = make_service(tmp_path, database=db)
        await svc1.reconcile_on_startup()
        info = await svc1.create_session("local")
        await svc1.close_session(str(info["session_id"]))
        await svc1.shutdown()

        svc2 = InteractiveTerminalService(
            state_dir=tmp_path / "state",
            terminal_log_dir=tmp_path / "different-terminals",
            events=EventBroker(),
            node_resolver=local_resolver,
            database=db,
        )
        with pytest.raises(ValueError, match="terminal_log_dir 已变更"):
            await svc2.reconcile_on_startup()
        await svc2.shutdown()

    asyncio.run(scenario())


def test_service_sessions_do_not_consume_executor_threads(tmp_path):
    async def scenario():
        svc = make_service(tmp_path)
        try:
            baseline = threading.active_count()
            for _ in range(3):
                await svc.create_session("local")
            await asyncio.sleep(0.3)
            assert threading.active_count() <= baseline + 2, (
                "创建交互终端会话不应派生过多常驻阻塞线程"
            )
        finally:
            await asyncio.wait_for(svc.shutdown(), timeout=15)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------


def make_config(tmp_path, *, port: int = 17861, state_name: str = "state") -> SchedulerConfig:
    state_dir = tmp_path / state_name
    return SchedulerConfig(
        host="127.0.0.1",
        port=port,
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        gpu_idle_required_checks=1,
        state_dir=state_dir,
        log_dir=state_dir / "logs",
        terminal_log_dir=state_dir / "terminals",
    )


def start_live_server(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = make_config(tmp_path, port=port, state_name="state-live")
    app = create_app(
        config,
        gpu_provider=lambda: [],
        autostart=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server, thread, port


def test_api_terminal_lifecycle_with_sse_stream(tmp_path):
    server, thread, port = start_live_server(tmp_path)
    base_url = f"http://127.0.0.1:{port}"
    try:
        with (
            httpx.Client(timeout=15.0, trust_env=False) as control,
            httpx.Client(timeout=15.0, trust_env=False) as stream_client,
        ):
            created = control.post(f"{base_url}/api/terminals", json={"node_id": "local"})
            created.raise_for_status()
            session = created.json()["session"]
            session_id = session["session_id"]
            assert session["node_id"] == "local"
            assert session["alive"] is True
            assert session["name"]

            listed = control.get(f"{base_url}/api/terminals").json()["sessions"]
            assert [item["session_id"] for item in listed] == [session_id]

            got_snapshot_done = False
            collected = bytearray()
            event_name: str | None = None
            deadline = time.time() + 15
            with stream_client.stream(
                "GET", f"{base_url}/api/terminals/{session_id}/stream"
            ) as stream:
                for line in stream.iter_lines():
                    assert time.time() < deadline, "SSE 流 15 秒内未完成"
                    if line.startswith("event: "):
                        event_name = line[len("event: "):]
                        continue
                    if not line.startswith("data: "):
                        continue
                    payload = json.loads(line[len("data: "):])
                    if event_name == "snapshot_done":
                        got_snapshot_done = True
                        data = base64.b64encode(b"echo api_test\n").decode("ascii")
                        resp = control.post(
                            f"{base_url}/api/terminals/{session_id}/input",
                            json={"data": data},
                        )
                        assert resp.status_code == 200
                    elif event_name == "chunk":
                        collected += base64.b64decode(payload["data"])
                        if b"api_test" in collected:
                            break
            assert got_snapshot_done
            assert b"api_test" in collected

            rename = control.patch(
                f"{base_url}/api/terminals/{session_id}",
                json={"name": "api-renamed"},
            )
            assert rename.status_code == 200
            assert rename.json()["session"]["name"] == "api-renamed"

            closed = control.delete(f"{base_url}/api/terminals/{session_id}")
            assert closed.status_code == 200
            assert control.get(f"{base_url}/api/terminals").json()["sessions"] == []

            archives = control.get(f"{base_url}/api/terminals/logs").json()["archives"]
            assert any(a["id"] == session_id for a in archives)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_api_terminal_startup_command_runs(tmp_path):
    server, thread, port = start_live_server(tmp_path)
    base_url = f"http://127.0.0.1:{port}"
    session_id: str | None = None
    marker = f"startup_{os.getpid()}_{int(time.time() * 1000)}"
    sentinel = tmp_path / f"{marker}.sentinel"
    try:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            created = client.post(
                f"{base_url}/api/terminals",
                json={
                    "node_id": "local",
                    "name": "startup-test",
                    "startup_command": (
                        f"printf '%s\\n' {marker}; "
                        f"touch {shlex.quote(str(sentinel))}\n"
                    ),
                },
            )
            created.raise_for_status()
            session_id = created.json()["session"]["session_id"]

            collected = b""
            deadline = time.time() + 8
            while time.time() < deadline:
                live = client.get(f"{base_url}/api/terminals/{session_id}/log?tail=8192")
                live.raise_for_status()
                collected = base64.b64decode(live.json()["data"])
                if marker.encode("utf-8") in collected and sentinel.exists():
                    break
                time.sleep(0.1)
            assert marker.encode("utf-8") in collected
            assert sentinel.exists()

    finally:
        if session_id is not None:
            try:
                with httpx.Client(timeout=5.0, trust_env=False) as client:
                    client.delete(f"{base_url}/api/terminals/{session_id}")
            except Exception:
                pass
        server.should_exit = True
        thread.join(timeout=10)


def test_api_input_validation_and_missing_resources(tmp_path):
    config = make_config(tmp_path)
    app = create_app(config, gpu_provider=lambda: [], autostart=True)
    with TestClient(app) as client:
        resp = client.post("/api/terminals", json={"node_id": "ghost"})
        assert resp.status_code == 404

        valid = base64.b64encode(b"ls\n").decode("ascii")
        resp = client.post("/api/terminals/no-such/input", json={"data": valid})
        assert resp.status_code == 404

        resp = client.post("/api/terminals/no-such/input", json={"data": "!!!"})
        assert resp.status_code == 400

        oversized = base64.b64encode(b"x" * (64 * 1024 + 1)).decode("ascii")
        resp = client.post("/api/terminals/no-such/input", json={"data": oversized})
        assert resp.status_code == 400
        assert "64KB" in resp.json()["detail"]


def test_api_rename_endpoint(tmp_path):
    server, thread, port = start_live_server(tmp_path)
    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            created = client.post(f"{base_url}/api/terminals", json={"node_id": "local", "name": "initial"})
            sid = created.json()["session"]["session_id"]

            renamed = client.patch(
                f"{base_url}/api/terminals/{sid}",
                json={"name": "new-name"},
            )
            assert renamed.status_code == 200
            assert renamed.json()["session"]["name"] == "new-name"

            empty = client.patch(
                f"{base_url}/api/terminals/{sid}",
                json={"name": ""},
            )
            assert empty.status_code == 422

            client.delete(f"{base_url}/api/terminals/{sid}")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_api_live_and_archived_log(tmp_path):
    server, thread, port = start_live_server(tmp_path)
    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            created = client.post(f"{base_url}/api/terminals", json={"node_id": "local"})
            sid = created.json()["session"]["session_id"]

            live = client.get(f"{base_url}/api/terminals/{sid}/log?tail=4096")
            assert live.status_code == 200
            assert "data" in live.json()

            client.delete(f"{base_url}/api/terminals/{sid}")

            archives = client.get(f"{base_url}/api/terminals/logs").json()["archives"]
            assert any(a["id"] == sid for a in archives)

            archived = client.get(f"{base_url}/api/terminals/logs/{sid}?tail=4096")
            assert archived.status_code == 200
            assert "data" in archived.json()
    finally:
        server.should_exit = True
        thread.join(timeout=10)


# ---------------------------------------------------------------------------
# conda inventory（与终端无关，保持原有测试）
# ---------------------------------------------------------------------------


class FakeNodesRegistry:
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
