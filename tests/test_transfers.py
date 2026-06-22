"""TransferService 集成测试：假 rsync/ssh 二进制 + TestClient 全链路。

假 rsync 以源路径中的标记决定行为（同一进程 env 无法按 job 区分）：
- 含 "hang" → sleep 300（测取消与进程组终止）
- 含 "fail" → 输出 code 23 stderr 后退出 23
- 含 "slow" → sleep 2s 后成功（测并发上限排队）
- 其余    → 输出两帧 progress2 后退出 0
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from exp_scheduler_app import transfer as transfer_mod
from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.database import Database, utc_now_iso
from exp_scheduler_app.web import create_app

from test_api import FakeGPUProvider, gpu, wait_for

FAKE_RSYNC_SCRIPT = """
import os, sys, time

paths = [a for a in sys.argv[1:] if not a.startswith("-")]
src = os.path.basename(paths[-2]) if len(paths) >= 2 else ""
mode = "success"
for cand in ("hang", "fail", "slow"):
    if cand in src:
        mode = cand
        break

if mode == "hang":
    time.sleep(300)
    sys.exit(0)
if mode == "fail":
    sys.stderr.write(
        'rsync: [sender] read errors mapping "/data/x": file has vanished\\n'
        "rsync error: some files/attrs were not transferred "
        "(see previous errors) (code 23) at main.c(1338) [sender=3.2.7]\\n"
    )
    sys.stderr.flush()
    sys.exit(23)
if mode == "slow":
    time.sleep(2.0)

sys.stdout.write("  1,234,567  45%  103.25MB/s    0:00:03\\r")
sys.stdout.flush()
time.sleep(0.05)
sys.stdout.write("  2,345,678 100%  120.00MB/s    0:00:00 (xfr#3, to-chk=0/5)\\r\\n")
sys.stdout.flush()
sys.exit(0)
"""

# 兜底假 ssh：本文件的用例不应触达真实网络，万一被调用立刻失败
FAKE_SSH_SCRIPT = """
import sys
sys.stderr.write("ssh: connect to host fake port 22: Connection refused\\n")
sys.exit(255)
"""


def _write_fake(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def make_config(tmp_path: Path) -> SchedulerConfig:
    return SchedulerConfig(
        host="127.0.0.1",
        port=17861,
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        gpu_idle_required_checks=1,
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "state" / "logs",
    )


def make_transfer_client(tmp_path: Path) -> TestClient:
    config = make_config(tmp_path)
    fake_rsync = _write_fake(tmp_path / "fake-rsync", FAKE_RSYNC_SCRIPT)
    fake_ssh = _write_fake(tmp_path / "fake-ssh", FAKE_SSH_SCRIPT)
    app = create_app(
        config,
        gpu_provider=FakeGPUProvider([gpu(0, idle=False)]),
        rsync_binary=fake_rsync,
        ssh_binary=fake_ssh,
        sshpass_binary=fake_ssh,
    )
    return TestClient(app)


def create_local_job(
    client: TestClient, tmp_path: Path, marker: str, **extra: object
) -> dict[str, object]:
    payload: dict[str, object] = {
        "src_node_id": "local",
        "src_path": str(tmp_path / f"src_{marker}"),
        "dst_node_id": "local",
        "dst_path": str(tmp_path / "dst"),
    }
    payload.update(extra)
    response = client.post("/api/transfers", json=payload)
    response.raise_for_status()
    return response.json()["job"]


def get_job(client: TestClient, job_id: str) -> dict[str, object]:
    response = client.get(f"/api/transfers/{job_id}")
    response.raise_for_status()
    return response.json()["job"]


def add_password_node(client: TestClient, name: str, host: str) -> str:
    response = client.post(
        "/api/nodes",
        json={
            "name": name,
            "host": host,
            "ssh_port": 22,
            "username": "ubuntu",
            "auth_method": "password",
            "password": "pw-secret",
        },
    )
    response.raise_for_status()
    return str(response.json()["node"]["id"])


def add_key_node(client: TestClient, tmp_path: Path, node_id: str, name: str) -> str:
    """直接向 DB 写入密钥认证节点（external 引用占位密钥，纯路由测试不触达 ssh）。"""
    database: Database = client.app.state.scheduler.database
    key_file = tmp_path / f"{node_id}.key"
    key_file.write_text("placeholder\n", encoding="utf-8")
    database.create_ssh_key(
        key_id=f"key-{node_id}",
        name=f"key-{name}",
        kind="external",
        key_path=str(key_file),
        public_key=None,
        fingerprint=None,
        notes=None,
    )
    database.create_node(
        node_id=node_id,
        name=name,
        host=f"{name}.example",
        ssh_port=22,
        username="ubuntu",
        auth_method="key",
        ssh_key_id=f"key-{node_id}",
        password=None,
        notes=None,
    )
    return node_id


def _group_dead(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _spawn_marker_process(*extra_args: str) -> subprocess.Popen[bytes]:
    """长睡眠子进程，argv 可附带标记项（如 agent sock 路径），模拟孤儿 ssh-agent。"""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", *extra_args]
    )


def _wait_cmdline_ready(pid: int, needle: str) -> None:
    """等待子进程 exec 完成（/proc/{pid}/cmdline 中出现标记项）。"""
    wait_for(lambda: transfer_mod._proc_cmdline_contains(pid, needle))


# ---------- 本机直跑（fake rsync）生命周期 ----------


def test_local_transfer_success_progress_and_log(tmp_path):
    with make_transfer_client(tmp_path) as client:
        job = create_local_job(client, tmp_path, "ok", name="同步测试")
        job_id = str(job["id"])
        assert job["route"] == "local"
        assert job["route_resolved_by"] == "auto"

        final = wait_for(
            lambda: (j := get_job(client, job_id))["status"] == "succeeded" and j
        )
        assert final["progress_percent"] == 100.0
        assert final["bytes_transferred"] == 2_345_678
        assert final["files_transferred"] == 3
        assert final["exit_code"] == 0
        assert final["error"] is None

        # 日志文件存在，头部含 redacted 命令行与节点快照
        log_path = Path(str(final["log_path"]))
        assert log_path.is_file()
        log = client.get(f"/api/transfers/{job_id}/log", params={"full": True})
        log.raise_for_status()
        content = log.json()["content"]
        assert "$ " in content and "fake-rsync" in content
        assert "节点快照" in content
        assert "SSHPASS" not in content


def test_local_transfer_failure_classified(tmp_path):
    with make_transfer_client(tmp_path) as client:
        job = create_local_job(client, tmp_path, "fail")
        job_id = str(job["id"])
        final = wait_for(
            lambda: (j := get_job(client, job_id))["status"] == "failed" and j
        )
        assert final["exit_code"] == 23
        assert final["error_code"] == "rsync_partial"
        assert "部分文件" in str(final["error"])
        # 非连接类错误不应触发路由降级
        assert [a["route"] for a in final["route_attempts"]] == ["local"]


def test_cancel_hanging_transfer_kills_process_group(tmp_path):
    with make_transfer_client(tmp_path) as client:
        job = create_local_job(client, tmp_path, "hang")
        job_id = str(job["id"])
        running = wait_for(
            lambda: (j := get_job(client, job_id))["status"] == "running"
            and j["pid"]
            and j
        )
        pid = int(running["pid"])

        cancel = client.post(f"/api/transfers/{job_id}/cancel")
        cancel.raise_for_status()
        final = wait_for(
            lambda: (j := get_job(client, job_id))["status"] == "cancelled" and j
        )
        assert final["finished_at"]
        assert final["error"] == "已取消"
        # 进程组已死（SIGINT 梯第一级即生效）
        wait_for(lambda: _group_dead(pid))

        # 终态任务不能再取消 → 409
        again = client.post(f"/api/transfers/{job_id}/cancel")
        assert again.status_code == 409


def test_concurrency_limit_queues_second_job(tmp_path):
    with make_transfer_client(tmp_path) as client:
        settings = client.put(
            "/api/transfers/settings", json={"max_concurrent_transfers": 1}
        )
        settings.raise_for_status()
        assert settings.json()["max_concurrent_transfers"] == 1

        first = create_local_job(client, tmp_path, "slow")
        first_id = str(first["id"])
        wait_for(lambda: get_job(client, first_id)["status"] == "running")

        second = create_local_job(client, tmp_path, "ok")
        second_id = str(second["id"])
        # 上限占满：第二个保持 pending（slow 任务至少跑 2s）
        assert get_job(client, second_id)["status"] == "pending"
        assert get_job(client, first_id)["status"] == "running"

        # 第一个完成后自动派发第二个
        wait_for(lambda: get_job(client, first_id)["status"] == "succeeded", timeout=10)
        wait_for(lambda: get_job(client, second_id)["status"] == "succeeded", timeout=10)


# ---------- 重启恢复 ----------


def test_restart_marks_running_jobs_interrupted(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.db_path)
    database.init()
    database.create_transfer_job(
        job_id="deadbeefcafe",
        name="重启前在跑",
        src_node_id="local",
        src_path="/tmp/a",
        dst_node_id="local",
        dst_path="/tmp/b",
        route="local",
        route_resolved_by="auto",
        rsync_args=[],
        delete_extras=False,
        dry_run=False,
        node_snapshot={},
    )
    database.update_transfer_job(
        "deadbeefcafe", status="running", started_at=utc_now_iso()
    )

    with make_transfer_client(tmp_path) as client:
        job = get_job(client, "deadbeefcafe")
        assert job["status"] == "interrupted"
        assert "中断" in str(job["error"])
        # interrupted 不自动重跑：active 列表为空
        listing = client.get("/api/transfers")
        listing.raise_for_status()
        assert listing.json()["active"] == []


# ---------- 路由 409 与 plan ----------


def test_double_password_nodes_create_conflict(tmp_path):
    with make_transfer_client(tmp_path) as client:
        node_a = add_password_node(client, "pw-node-a", "10.9.9.1")
        node_b = add_password_node(client, "pw-node-b", "10.9.9.2")
        response = client.post(
            "/api/transfers",
            json={
                "src_node_id": node_a,
                "src_path": "/data/x",
                "dst_node_id": node_b,
                "dst_path": "/data/y",
            },
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "没有可用的传输路由" in detail
        # detail 含各候选不可行原因
        assert "direct_from_src" in detail and "bridged_push" in detail
        assert "密码" in detail


def test_plan_reports_needs_probe_for_unknown_edges(tmp_path):
    with make_transfer_client(tmp_path) as client:
        add_key_node(client, tmp_path, "nodea", "key-node-a")
        add_key_node(client, tmp_path, "nodeb", "key-node-b")
        response = client.post(
            "/api/transfers/plan",
            json={
                "src_node_id": "nodea",
                "src_path": "/data/x",
                "dst_node_id": "nodeb",
                "dst_path": "/data/y",
            },
        )
        response.raise_for_status()
        plan = response.json()
        assert plan["needs_probe"] is True
        assert plan["recommended"] == "direct_from_src"
        cand = next(c for c in plan["candidates"] if c["route"] == "direct_from_src")
        assert cand["requires_probe"] == [["local", "nodea"], ["nodea", "nodeb"]]

        # local→local 始终可行且无需探测
        trivial = client.post(
            "/api/transfers/plan",
            json={
                "src_node_id": "local",
                "src_path": "/tmp/a",
                "dst_node_id": "local",
                "dst_path": "/tmp/b",
            },
        )
        trivial.raise_for_status()
        assert trivial.json() == {
            "candidates": [
                {"route": "local", "feasible": True, "reasons": [], "requires_probe": []}
            ],
            "recommended": "local",
            "needs_probe": False,
        }


def test_plan_same_remote_node_returns_empty_with_reason(tmp_path):
    """plan 与 create 契约一致：src==dst 同一远程节点时返回空候选 + 明确中文原因。"""
    with make_transfer_client(tmp_path) as client:
        add_key_node(client, tmp_path, "nodea", "key-node-a")
        response = client.post(
            "/api/transfers/plan",
            json={
                "src_node_id": "nodea",
                "src_path": "/data/x",
                "dst_node_id": "nodea",
                "dst_path": "/data/y",
            },
        )
        response.raise_for_status()
        plan = response.json()
        assert plan["candidates"] == []
        assert plan["recommended"] is None
        assert plan["needs_probe"] is False
        assert "同一远程节点" in plan["reason"]

        # create 同样拒绝，提示一致
        create = client.post(
            "/api/transfers",
            json={
                "src_node_id": "nodea",
                "src_path": "/data/x",
                "dst_node_id": "nodea",
                "dst_path": "/data/y",
            },
        )
        assert create.status_code == 400
        assert "同一远程节点" in create.json()["detail"]


# ---------- 取消竞态（spawn 准备窗口） ----------


def test_cancel_landing_in_spawn_window_still_kills_process(tmp_path, monkeypatch):
    """取消落在准备窗口（cancel_requested 已置位但 process 尚未赋值）时，
    _spawn_and_watch 必须在进程启动后立即补走取消梯，而不是让传输跑完全程。"""
    with make_transfer_client(tmp_path) as client:
        service = client.app.state.transfer
        real_popen = transfer_mod.subprocess.Popen
        captured: dict[str, int] = {}

        def popen_hook(argv, *args, **kwargs):
            proc = real_popen(argv, *args, **kwargs)
            if "fake-rsync" in str(argv[0]):
                captured["pid"] = proc.pid
                # 模拟取消请求恰好落在 Popen 返回与 handle.process 赋值之间
                for handle in service._handles.values():
                    handle.cancel_requested = True
            return proc

        monkeypatch.setattr(transfer_mod.subprocess, "Popen", popen_hook)
        job = create_local_job(client, tmp_path, "hang")
        job_id = str(job["id"])
        final = wait_for(
            lambda: (j := get_job(client, job_id))["status"] == "cancelled" and j
        )
        assert final["error"] == "已取消"
        # hang 模式 sleep 300：若未补发信号，进程组会一直活着
        wait_for(lambda: _group_dead(captured["pid"]))


def test_repeat_cancel_resends_signal_when_first_cancel_missed_process(tmp_path):
    """首次取消落在准备窗口（仅置位未发信号、无取消梯在途）后，
    再次取消必须补发信号，而不是因 cancel_requested 已置位而 no-op。"""
    with make_transfer_client(tmp_path) as client:
        service = client.app.state.transfer
        job = create_local_job(client, tmp_path, "hang")
        job_id = str(job["id"])
        running = wait_for(
            lambda: (j := get_job(client, job_id))["status"] == "running"
            and j["pid"]
            and j
        )
        pid = int(running["pid"])
        # 模拟历史竞态残留：cancel_requested 已置位但从未对进程发过信号
        handle = service._handles[job_id]
        assert handle.cancel_ladder is None
        handle.cancel_requested = True

        again = client.post(f"/api/transfers/{job_id}/cancel")
        again.raise_for_status()
        final = wait_for(
            lambda: (j := get_job(client, job_id))["status"] == "cancelled" and j
        )
        assert final["error"] == "已取消"
        wait_for(lambda: _group_dead(pid))


# ---------- agent socket 目录防抢占 ----------


def test_ensure_private_dir_rejects_foreign_owner(tmp_path, monkeypatch):
    """回落目录已被他人抢占（属主不符）时拒绝使用，抛中文错误。"""
    target = tmp_path / "fallback-dir"
    target.mkdir(mode=0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(transfer_mod.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ValueError, match="属主"):
        transfer_mod._ensure_private_dir(target)


def test_ensure_private_dir_tightens_loose_mode(tmp_path):
    """目录权限过宽时收紧为 0700（chmod 不再静默吞错）。"""
    target = tmp_path / "fallback-dir"
    target.mkdir()
    os.chmod(target, 0o755)
    transfer_mod._ensure_private_dir(target)
    assert (os.stat(target).st_mode & 0o777) == 0o700


# ---------- 临时 ssh-agent 泄漏回收 ----------


def test_start_job_agent_failure_reaps_daemonized_agent_by_sock(tmp_path):
    """ssh-agent 启动后 PID 解析失败：按 sock 路径在 /proc 中回收已 daemonize 的 agent。"""
    with make_transfer_client(tmp_path) as client:
        service = client.app.state.transfer
        job_id = "feedfacefeed"
        sock = service.state_dir / "run" / f"agent-{job_id}.sock"
        leaked = _spawn_marker_process("-a", str(sock))
        try:
            _wait_cmdline_ready(leaked.pid, str(sock))

            async def fake_run_local(argv, *, env_extra=None, timeout_seconds=10.0):
                return 0, "garbage output without agent pid", ""

            service._run_local = fake_run_local  # type: ignore[method-assign]
            with pytest.raises(ValueError, match="无法解析"):
                asyncio.run(service._start_job_agent(job_id, "/nonexistent/key"))
            # 兜底回收：cmdline 含 sock 路径的"agent"被 SIGTERM
            assert leaked.wait(timeout=10) == -15
        finally:
            if leaked.poll() is None:
                leaked.kill()


def test_startup_sweeps_orphan_agents_in_run_and_fallback_dirs(tmp_path, monkeypatch):
    """startup 清扫 state_dir/run 与 /tmp 回落目录的 agent-*.sock 孤儿，
    并按 /proc cmdline 含 sock 路径回收对应进程。"""
    tmp_root = tmp_path / "tmproot"
    monkeypatch.setattr(transfer_mod, "_FALLBACK_TMP_ROOT", tmp_root)
    run_dir = tmp_path / "state" / "run"
    run_dir.mkdir(parents=True)
    fallback_dir = tmp_root / f"exp-sched-{os.getuid()}"
    fallback_dir.mkdir(parents=True)
    socks = [run_dir / "agent-aaaa.sock", fallback_dir / "agent-bbbb.sock"]
    leaked: list[subprocess.Popen[bytes]] = []
    try:
        for sock in socks:
            sock.touch()
            proc = _spawn_marker_process("-a", str(sock))
            leaked.append(proc)
            _wait_cmdline_ready(proc.pid, str(sock))
        with make_transfer_client(tmp_path):
            for proc in leaked:
                assert proc.wait(timeout=10) == -15
            for sock in socks:
                assert not sock.exists()
    finally:
        for proc in leaked:
            if proc.poll() is None:
                proc.kill()


def test_startup_reaps_agent_pid_only_when_cmdline_matches(tmp_path):
    """重启恢复时 agent_pid 校验 cmdline 含该 job 专属 sock 路径：
    匹配则回收，不匹配（pid 复用给无关进程）只清记录不杀进程。"""
    config = make_config(tmp_path)
    database = Database(config.db_path)
    database.init()
    matching_job = "aaaabbbbcccc"
    decoy_job = "ddddeeeeffff"
    for job_id in (matching_job, decoy_job):
        database.create_transfer_job(
            job_id=job_id,
            name=None,
            src_node_id="local",
            src_path="/tmp/a",
            dst_node_id="local",
            dst_path="/tmp/b",
            route="local",
            route_resolved_by="auto",
            rsync_args=[],
            delete_extras=False,
            dry_run=False,
            node_snapshot={},
        )
    sock_arg = str(tmp_path / "state" / "run" / f"agent-{matching_job}.sock")
    matching_proc = _spawn_marker_process("-a", sock_arg)
    decoy_proc = _spawn_marker_process()
    try:
        _wait_cmdline_ready(matching_proc.pid, sock_arg)
        database.update_transfer_job(
            matching_job,
            status="running",
            started_at=utc_now_iso(),
            agent_pid=matching_proc.pid,
        )
        database.update_transfer_job(
            decoy_job,
            status="running",
            started_at=utc_now_iso(),
            agent_pid=decoy_proc.pid,
        )
        with make_transfer_client(tmp_path) as client:
            # cmdline 含专属 sock 路径：回收
            assert matching_proc.wait(timeout=10) == -15
            # cmdline 不含（pid 已复用给无关进程）：存活，只清记录
            assert decoy_proc.poll() is None
            assert get_job(client, matching_job)["agent_pid"] is None
            assert get_job(client, decoy_job)["agent_pid"] is None
    finally:
        for proc in (matching_proc, decoy_proc):
            if proc.poll() is None:
                proc.kill()


# ---------- 专属线程池 ----------


def test_transfer_uses_dedicated_executor(tmp_path):
    """阻塞 reader/process.wait 跑在传输专属线程池（8×3+4），不与全局默认池抢线程。"""
    with make_transfer_client(tmp_path) as client:
        service = client.app.state.transfer
        assert (
            service._executor._max_workers
            == transfer_mod._MAX_TRANSFER_CONCURRENCY_CAP * 3 + 4
            == 28
        )
        job = create_local_job(client, tmp_path, "ok")
        wait_for(lambda: get_job(client, str(job["id"]))["status"] == "succeeded")
        # 传输期间的阻塞调用确实落入专属池：池内出现 transfer-io 命名线程
        assert any(
            t.name.startswith("transfer-io") for t in service._executor._threads
        )
    # 服务停机后专属池随之关闭
    assert service._executor._shutdown is True
