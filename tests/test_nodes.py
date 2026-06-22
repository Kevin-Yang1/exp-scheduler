"""节点注册表 / SSH 密钥库 / 连通性探测测试。

通过 tmp_path/bin 下的可执行 Python 假二进制（ssh / ssh-keygen / ssh-agent /
ssh-add / sshpass）模拟外部命令，按 argv 标记与环境变量输出预设内容，
覆盖密钥库 CRUD、节点 CRUD 脱敏、测试连接、hop 探测标记判读与纯函数命令构造。
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sqlite3
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.database import LOCAL_NODE_ID, Database
from exp_scheduler_app.events import EventBroker
from exp_scheduler_app.nodes import (
    NodeRegistryService,
    ResolvedAuth,
    build_ssh_command,
    build_ssh_option_args,
    host_key_alias,
)
from exp_scheduler_app.web import create_app

from test_api import FakeGPUProvider, gpu

SERVER_NAME = "主控机"

# 私钥正文标记：用于断言任何 API 响应中不出现私钥内容
PRIVATE_KEY_BODY = "RkFLRUtFWUJPRFlGQUtFS0VZQk9EWQ=="
SAMPLE_PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    f"{PRIVATE_KEY_BODY}\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)

FAKE_SSH_SCRIPT = """
import json, os, sys

log_path = os.environ.get("FAKE_SSH_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "argv": sys.argv[1:],
            "has_auth_sock": "SSH_AUTH_SOCK" in os.environ,
        }, ensure_ascii=False) + "\\n")

mode = os.environ.get("FAKE_SSH_MODE", "ok")
joined = " ".join(sys.argv[1:])

if mode == "denied":
    sys.stderr.write("ubuntu@host: Permission denied (publickey,password).\\n")
    sys.exit(255)

if "EXP_SCHED_HOP_OK" in joined:
    hop = os.environ.get("FAKE_SSH_HOP", "ok-ok")
    if hop == "hop-fail":
        sys.stderr.write("ssh: connect to host gateway port 22: Connection timed out\\n")
        sys.exit(255)
    sys.stdout.write("EXP_SCHED_HOP_OK\\n")
    if hop == "ok-fail":
        sys.stdout.write("EXP_SCHED_LINK_FAIL rc=255\\n")
        sys.stderr.write("ssh: connect to host node-b port 22: Connection refused\\n")
        sys.exit(0)
    sys.stdout.write("EXP_SCHED_LINK_OK\\n")
    sys.exit(0)

if "EXP_SCHED_OK" in joined:
    sys.stdout.write("EXP_SCHED_OK\\n")
    sys.stdout.write("rsync  version 3.2.7  protocol version 31\\n")
    sys.stdout.write("HAS_SSHPASS\\n")
    sys.exit(0)

if "RC=$?" in joined:
    sys.stdout.write("RC=1\\n")
    sys.exit(0)

# 端口转发探针（remote_command == "true"）等：直接成功
sys.exit(0)
"""

FAKE_SSH_KEYGEN_SCRIPT = """
import os, sys

if os.environ.get("FAKE_KEYGEN_FAIL") == "1":
    sys.stderr.write("Load key: invalid format\\n")
    sys.exit(1)
args = sys.argv[1:]
if "-y" in args:
    sys.stdout.write("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKEPUBKEY fake@test\\n")
    sys.exit(0)
if "-lf" in args:
    sys.stdout.write("256 SHA256:FAKEFINGERPRINT1234 fake@test (ED25519)\\n")
    sys.exit(0)
sys.exit(0)
"""

FAKE_SSH_AGENT_SCRIPT = """
import os, sys
# 模拟真实 ssh-agent 创建 -a 指定的 socket（touch 占位），便于测试 sock 回收逻辑
args = sys.argv[1:]
if "-a" in args:
    open(args[args.index("-a") + 1], "w").close()
if os.environ.get("FAKE_AGENT_OUTPUT") == "garbage":
    sys.stdout.write("agent started but no pid line\\n")
    sys.exit(0)
# 输出自身 PID：脚本退出后该 PID 已消亡，_stop_temp_agent 的 kill 会被静默吞掉
sys.stdout.write("SSH_AUTH_SOCK=/tmp/fake.sock; export SSH_AUTH_SOCK;\\n")
sys.stdout.write(f"SSH_AGENT_PID={os.getpid()}; export SSH_AGENT_PID;\\n")
sys.exit(0)
"""

FAKE_SSH_ADD_SCRIPT = """
import os, sys
log_path = os.environ.get("FAKE_SSH_ADD_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(" ".join(sys.argv[1:]) + " sock=" + os.environ.get("SSH_AUTH_SOCK", "") + "\\n")
if os.environ.get("FAKE_SSH_ADD_FAIL") == "1":
    sys.stderr.write("Could not add identity\\n")
    sys.exit(1)
# -l 查询返回 1（agent 可达但为空），加载密钥返回 0
sys.exit(1 if "-l" in sys.argv[1:] else 0)
"""

FAKE_SSHPASS_SCRIPT = """
import os, sys
args = sys.argv[1:]
if args and args[0] == "-e":
    args = args[1:]
os.execv(args[0], args)
"""


def install_fake_bins(tmp_path: Path) -> Path:
    """在 tmp_path/bin 下写入全套假二进制，返回 bin 目录。"""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    scripts = {
        "ssh": FAKE_SSH_SCRIPT,
        "ssh-keygen": FAKE_SSH_KEYGEN_SCRIPT,
        "ssh-agent": FAKE_SSH_AGENT_SCRIPT,
        "ssh-add": FAKE_SSH_ADD_SCRIPT,
        "sshpass": FAKE_SSHPASS_SCRIPT,
    }
    for name, body in scripts.items():
        path = bindir / name
        path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(0o755)
    return bindir


def make_api_client(tmp_path: Path, monkeypatch) -> TestClient:
    """带假二进制注入的 TestClient（ssh-agent / ssh-add 走 PATH 解析）。"""
    bindir = install_fake_bins(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    config = SchedulerConfig(
        host="127.0.0.1",
        port=17861,
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        gpu_idle_required_checks=1,
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "state" / "logs",
        server_name=SERVER_NAME,
    )
    provider = FakeGPUProvider([gpu(0, idle=False)])
    app = create_app(
        config,
        gpu_provider=provider,
        ssh_binary=str(bindir / "ssh"),
        ssh_keygen_binary=str(bindir / "ssh-keygen"),
        sshpass_binary=str(bindir / "sshpass"),
    )
    return TestClient(app)


def make_registry(tmp_path: Path, monkeypatch) -> tuple[NodeRegistryService, Database]:
    """直接构造 NodeRegistryService（绕过 web 层，用于 probe_edge 单元测试）。"""
    bindir = install_fake_bins(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    database = Database(tmp_path / "state" / "scheduler.db")
    database.init()
    registry = NodeRegistryService(
        database=database,
        events=EventBroker(),
        state_dir=tmp_path / "state",
        server_name=SERVER_NAME,
        ssh_binary=str(bindir / "ssh"),
        ssh_keygen_binary=str(bindir / "ssh-keygen"),
        sshpass_binary=str(bindir / "sshpass"),
    )
    return registry, database


def add_key_node(database: Database, tmp_path: Path, node_id: str, name: str) -> None:
    """直接向 DB 写入一个密钥认证节点（external 引用 tmp 下的占位密钥文件）。"""
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


def create_key_via_api(client: TestClient, name: str = "deploy-key") -> dict[str, object]:
    response = client.post(
        "/api/ssh-keys",
        json={"name": name, "private_key": SAMPLE_PRIVATE_KEY},
    )
    assert response.status_code == 200, response.text
    return response.json()["key"]


# ---------- 1. SSH 密钥库 ----------


def test_create_managed_key_success(tmp_path, monkeypatch):
    with make_api_client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/ssh-keys",
            json={"name": "deploy-key", "private_key": SAMPLE_PRIVATE_KEY},
        )
        assert response.status_code == 200, response.text
        # 响应含指纹/公钥，绝不含私钥正文
        assert PRIVATE_KEY_BODY not in response.text
        key = response.json()["key"]
        assert key["kind"] == "managed"
        assert key["fingerprint"] == "256 SHA256:FAKEFINGERPRINT1234 fake@test (ED25519)"
        assert key["public_key"].startswith("ssh-ed25519 ")

        # 托管私钥文件落在 state_dir/keys 下，权限严格 0600，内容为粘贴的私钥
        key_path = Path(str(key["key_path"]))
        assert key_path.parent == tmp_path / "state" / "keys"
        assert key_path.is_file()
        assert (key_path.stat().st_mode & 0o777) == 0o600
        assert PRIVATE_KEY_BODY in key_path.read_text(encoding="utf-8")

        # 列表接口同样不泄露私钥正文
        listing = client.get("/api/ssh-keys")
        assert listing.status_code == 200
        assert PRIVATE_KEY_BODY not in listing.text
        assert any(item["id"] == key["id"] for item in listing.json()["keys"])


def test_create_managed_key_invalid_content(tmp_path, monkeypatch):
    with make_api_client(tmp_path, monkeypatch) as client:
        monkeypatch.setenv("FAKE_KEYGEN_FAIL", "1")
        response = client.post(
            "/api/ssh-keys",
            json={"name": "bad-key", "private_key": "not a key"},
        )
        assert response.status_code == 400
        assert "私钥校验失败" in response.json()["detail"]
        # 校验失败的托管文件必须被回收
        keys_dir = tmp_path / "state" / "keys"
        assert not keys_dir.exists() or not any(keys_dir.iterdir())


def test_create_external_key_missing_path(tmp_path, monkeypatch):
    with make_api_client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/ssh-keys",
            json={
                "name": "ext-key",
                "private_key_path": str(tmp_path / "no-such-key"),
            },
        )
        assert response.status_code == 400
        assert "无法读取私钥文件" in response.json()["detail"]


def test_delete_key_referenced_by_node_conflict(tmp_path, monkeypatch):
    with make_api_client(tmp_path, monkeypatch) as client:
        key = create_key_via_api(client)
        response = client.post(
            "/api/nodes",
            json={
                "name": "node-a",
                "host": "10.0.0.8",
                "username": "ubuntu",
                "auth_method": "key",
                "ssh_key_id": key["id"],
            },
        )
        assert response.status_code == 200, response.text

        deletion = client.delete(f"/api/ssh-keys/{key['id']}")
        assert deletion.status_code == 409
        assert "正被" in deletion.json()["detail"]
        # 密钥仍在
        assert any(item["id"] == key["id"] for item in client.get("/api/ssh-keys").json()["keys"])


# ---------- 2. 节点 CRUD ----------


def test_node_crud_local_pseudo_node_and_sanitization(tmp_path, monkeypatch):
    with make_api_client(tmp_path, monkeypatch) as client:
        key = create_key_via_api(client)

        created = client.post(
            "/api/nodes",
            json={
                "name": "node-key",
                "host": "10.0.0.8",
                "ssh_port": 2222,
                "username": "ubuntu",
                "auth_method": "key",
                "ssh_key_id": key["id"],
            },
        )
        assert created.status_code == 200, created.text
        key_node = created.json()["node"]
        assert "password" not in key_node
        assert key_node["has_password"] is False
        assert key_node["is_local"] is False

        password_value = "s3cret-pass-9527"
        created_pw = client.post(
            "/api/nodes",
            json={
                "name": "node-pw",
                "host": "10.0.0.9",
                "username": "ubuntu",
                "auth_method": "password",
                "password": password_value,
            },
        )
        assert created_pw.status_code == 200, created_pw.text
        assert password_value not in created_pw.text
        pw_node = created_pw.json()["node"]
        assert "password" not in pw_node
        assert pw_node["has_password"] is True

        # 列表首项为 local 伪节点，明文密码不出现在任何响应
        listing = client.get("/api/nodes")
        assert listing.status_code == 200
        assert password_value not in listing.text
        nodes = listing.json()["nodes"]
        assert nodes[0] == {"id": LOCAL_NODE_ID, "name": SERVER_NAME, "is_local": True}
        assert {node["name"] for node in nodes[1:]} == {"node-key", "node-pw"}
        assert all("password" not in node for node in nodes[1:])

        # 名称重复 → 400
        duplicated = client.post(
            "/api/nodes",
            json={
                "name": "node-key",
                "host": "10.0.0.10",
                "username": "ubuntu",
                "auth_method": "key",
                "ssh_key_id": key["id"],
            },
        )
        assert duplicated.status_code == 400
        assert "已存在" in duplicated.json()["detail"]

        # 名称 local 为保留名 → 400
        reserved = client.post(
            "/api/nodes",
            json={
                "name": "local",
                "host": "10.0.0.11",
                "username": "ubuntu",
                "auth_method": "key",
                "ssh_key_id": key["id"],
            },
        )
        assert reserved.status_code == 400
        assert "local" in reserved.json()["detail"]


def test_delete_node_removes_link_edges(tmp_path, monkeypatch):
    with make_api_client(tmp_path, monkeypatch) as client:
        key = create_key_via_api(client)
        node = client.post(
            "/api/nodes",
            json={
                "name": "node-a",
                "host": "10.0.0.8",
                "username": "ubuntu",
                "auth_method": "key",
                "ssh_key_id": key["id"],
            },
        ).json()["node"]

        # 通过测试连接生成 local→node 连通边
        tested = client.post(f"/api/nodes/{node['id']}/test")
        assert tested.status_code == 200, tested.text
        links = client.get("/api/nodes/links").json()["links"]
        assert any(link["to_node_id"] == node["id"] for link in links)

        deletion = client.delete(f"/api/nodes/{node['id']}")
        assert deletion.status_code == 200
        links_after = client.get("/api/nodes/links").json()["links"]
        assert not any(
            link["from_node_id"] == node["id"] or link["to_node_id"] == node["id"]
            for link in links_after
        )


def test_update_node_switch_to_key_clears_password(tmp_path, monkeypatch):
    """password→key 切换且不传 password 时，DB 中的旧明文密码必须被清除。"""
    with make_api_client(tmp_path, monkeypatch) as client:
        key = create_key_via_api(client)
        password_value = "s3cret-PLAINTEXT"
        node = client.post(
            "/api/nodes",
            json={
                "name": "node-switch",
                "host": "10.0.0.12",
                "username": "ubuntu",
                "auth_method": "password",
                "password": password_value,
            },
        ).json()["node"]
        assert node["has_password"] is True

        # 切换为 key 认证，password 字段不传（NodeRequest.password 默认 None）
        updated = client.put(
            f"/api/nodes/{node['id']}",
            json={
                "name": "node-switch",
                "host": "10.0.0.12",
                "username": "ubuntu",
                "auth_method": "key",
                "ssh_key_id": key["id"],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["node"]["has_password"] is False

        # DB 列层面旧口令不得驻留
        conn = sqlite3.connect(tmp_path / "state" / "scheduler.db")
        try:
            row = conn.execute(
                "SELECT password FROM nodes WHERE id = ?", (node["id"],)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None and row[0] is None

        # 列表接口同样不再误报 has_password
        nodes = client.get("/api/nodes").json()["nodes"]
        stored = next(item for item in nodes if item.get("id") == node["id"])
        assert stored["has_password"] is False


# ---------- 3. 测试连接 ----------


def test_node_test_connection_success(tmp_path, monkeypatch):
    with make_api_client(tmp_path, monkeypatch) as client:
        key = create_key_via_api(client)
        node = client.post(
            "/api/nodes",
            json={
                "name": "node-a",
                "host": "10.0.0.8",
                "username": "ubuntu",
                "auth_method": "key",
                "ssh_key_id": key["id"],
            },
        ).json()["node"]

        response = client.post(f"/api/nodes/{node['id']}/test")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["ok"] is True
        assert result["rsync_version"].startswith("rsync")
        assert result["has_sshpass"] is True
        assert result["tcp_forward_ok"] is True
        assert result["agent_forward_ok"] is True
        assert "连接成功" in result["detail"]

        # 节点能力列已更新
        nodes = client.get("/api/nodes").json()["nodes"]
        stored = next(item for item in nodes if item.get("id") == node["id"])
        assert stored["rsync_version"].startswith("rsync")
        assert stored["has_sshpass"] is True
        assert stored["tcp_forward_ok"] is True
        assert stored["agent_forward_ok"] is True

        # node_links 出现 local→node 的 ok 直连边
        links = client.get("/api/nodes/links").json()["links"]
        link = next(
            item
            for item in links
            if item["from_node_id"] == LOCAL_NODE_ID and item["to_node_id"] == node["id"]
        )
        assert link["status"] == "ok"
        assert link["probe_method"] == "direct"
        assert link["last_error"] is None


def test_node_test_connection_auth_failure(tmp_path, monkeypatch):
    with make_api_client(tmp_path, monkeypatch) as client:
        key = create_key_via_api(client)
        node = client.post(
            "/api/nodes",
            json={
                "name": "node-a",
                "host": "10.0.0.8",
                "username": "ubuntu",
                "auth_method": "key",
                "ssh_key_id": key["id"],
            },
        ).json()["node"]

        monkeypatch.setenv("FAKE_SSH_MODE", "denied")
        response = client.post(f"/api/nodes/{node['id']}/test")
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["ok"] is False
        assert "认证失败" in result["detail"]

        links = client.get("/api/nodes/links").json()["links"]
        link = next(
            item
            for item in links
            if item["from_node_id"] == LOCAL_NODE_ID and item["to_node_id"] == node["id"]
        )
        assert link["status"] == "failed"
        assert "认证失败" in link["last_error"]


# ---------- 4. probe_edge hop 探测标记判读 ----------


def test_probe_edge_hop_ok(tmp_path, monkeypatch):
    registry, database = make_registry(tmp_path, monkeypatch)
    add_key_node(database, tmp_path, "node-a", "node-a")
    add_key_node(database, tmp_path, "node-b", "node-b")
    ssh_log = tmp_path / "ssh.log"
    add_log = tmp_path / "ssh-add.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(ssh_log))
    monkeypatch.setenv("FAKE_SSH_ADD_LOG", str(add_log))
    monkeypatch.setenv("FAKE_SSH_HOP", "ok-ok")

    link = asyncio.run(registry.probe_edge("node-a", "node-b"))
    assert link["status"] == "ok"
    assert link["probe_method"] == "hop"
    assert link["last_error"] is None
    stored = database.get_node_link("node-a", "node-b")
    assert stored is not None and stored["status"] == "ok"

    # hop 命令开启了 agent 转发（-A）且目标别名经 HostKeyAlias 传递
    calls = [json.loads(line) for line in ssh_log.read_text(encoding="utf-8").splitlines()]
    hop_call = next(call for call in calls if "EXP_SCHED_HOP_OK" in " ".join(call["argv"]))
    assert "-A" in hop_call["argv"]
    assert hop_call["has_auth_sock"] is True
    assert host_key_alias("node-b") in hop_call["argv"][-1]
    assert "ubuntu@node-a.example" in hop_call["argv"]

    # 目标节点密钥已通过 ssh-add 加载进临时 agent
    add_calls = add_log.read_text(encoding="utf-8")
    assert str(tmp_path / "node-b.key") in add_calls


def test_probe_edge_hop_link_fail(tmp_path, monkeypatch):
    registry, database = make_registry(tmp_path, monkeypatch)
    add_key_node(database, tmp_path, "node-a", "node-a")
    add_key_node(database, tmp_path, "node-b", "node-b")
    monkeypatch.setenv("FAKE_SSH_HOP", "ok-fail")

    link = asyncio.run(registry.probe_edge("node-a", "node-b"))
    assert link["status"] == "failed"
    assert link["probe_method"] == "hop"
    assert "目标 SSH 退出码 255" in link["last_error"]
    assert "网络不可达或 sshd 未运行" in link["last_error"]
    stored = database.get_node_link("node-a", "node-b")
    assert stored is not None and stored["status"] == "failed"


def test_probe_edge_hop_unreachable_keeps_unknown(tmp_path, monkeypatch):
    registry, database = make_registry(tmp_path, monkeypatch)
    add_key_node(database, tmp_path, "node-a", "node-a")
    add_key_node(database, tmp_path, "node-b", "node-b")
    monkeypatch.setenv("FAKE_SSH_HOP", "hop-fail")

    link = asyncio.run(registry.probe_edge("node-a", "node-b"))
    # 主控→发起端这一跳失败：from→to 维持 unknown，并顺手回写 local→from 失败
    assert link["status"] == "unknown"
    assert "经主控无法连接发起端" in link["last_error"]
    assert "网络不可达或连接超时" in link["last_error"]
    hop_link = database.get_node_link(LOCAL_NODE_ID, "node-a")
    assert hop_link is not None
    assert hop_link["status"] == "failed"
    assert "网络不可达或连接超时" in hop_link["last_error"]


def test_probe_edge_hop_quotes_target(tmp_path, monkeypatch):
    """hop snippet 中的 user@host 必须 shlex.quote（防御纵深，不依赖正则白名单）。"""
    registry, database = make_registry(tmp_path, monkeypatch)
    add_key_node(database, tmp_path, "node-a", "node-a")
    # IPv6 字面量含 [ ]（HOST_PATTERN 允许），shlex.quote 后应带引号进入内层 snippet
    key_file = tmp_path / "node-b6.key"
    key_file.write_text("placeholder\n", encoding="utf-8")
    database.create_ssh_key(
        key_id="key-node-b6",
        name="key-node-b6",
        kind="external",
        key_path=str(key_file),
        public_key=None,
        fingerprint=None,
        notes=None,
    )
    database.create_node(
        node_id="node-b6",
        name="node-b6",
        host="[::1]",
        ssh_port=22,
        username="ubuntu",
        auth_method="key",
        ssh_key_id="key-node-b6",
        password=None,
        notes=None,
    )
    ssh_log = tmp_path / "ssh.log"
    monkeypatch.setenv("FAKE_SSH_LOG", str(ssh_log))
    monkeypatch.setenv("FAKE_SSH_HOP", "ok-ok")

    link = asyncio.run(registry.probe_edge("node-a", "node-b6"))
    assert link["status"] == "ok"

    calls = [json.loads(line) for line in ssh_log.read_text(encoding="utf-8").splitlines()]
    hop_call = next(call for call in calls if "EXP_SCHED_HOP_OK" in " ".join(call["argv"]))
    inner = shlex.split(hop_call["argv"][-1])
    assert inner[:2] == ["sh", "-c"]
    # user@host 作为单个被引用的词出现，内层 sh 不会再做分词/通配展开
    assert shlex.quote("ubuntu@[::1]") == "'ubuntu@[::1]'"
    assert "'ubuntu@[::1]'" in inner[2]


# ---------- 5. lookup_host_key_lines 大小写不敏感 ----------


def test_lookup_host_key_lines_case_insensitive(tmp_path, monkeypatch):
    """节点 host 含大写时也必须命中 OpenSSH 写入的全小写 known_hosts 行。"""
    registry, _database = make_registry(tmp_path, monkeypatch)
    known_hosts = registry.known_hosts_path()
    known_hosts.write_text(
        "gpu-server.lab ssh-ed25519 AAAA-default-port\n"
        "[gpu-server.lab]:2222 ssh-rsa BBBB-alt-port\n",
        encoding="utf-8",
    )
    assert registry.lookup_host_key_lines("GPU-Server.Lab", 22, "alias-x") == [
        "alias-x ssh-ed25519 AAAA-default-port"
    ]
    assert registry.lookup_host_key_lines("GPU-Server.Lab", 2222, "alias-x") == [
        "alias-x ssh-rsa BBBB-alt-port"
    ]
    # 反向：known_hosts 行含大写同样不敏感
    known_hosts.write_text("GPU-SERVER.LAB ssh-ed25519 CCCC\n", encoding="utf-8")
    assert registry.lookup_host_key_lines("gpu-server.lab", 22, "alias-y") == [
        "alias-y ssh-ed25519 CCCC"
    ]


# ---------- 6. probe_links 防抖语义 ----------


def test_probe_links_disjoint_pairs_not_debounced(tmp_path, monkeypatch):
    """在途 sweep 期间请求不相交的边集：必须为差集边另起 sweep，而非被防抖吞掉。"""
    registry, database = make_registry(tmp_path, monkeypatch)
    add_key_node(database, tmp_path, "node-a", "node-a")
    add_key_node(database, tmp_path, "node-b", "node-b")

    async def scenario() -> None:
        probed: list[tuple[str, str]] = []
        gate = asyncio.Event()

        async def fake_probe_edge(from_id, to_id, *, record_operation=True):
            probed.append((from_id, to_id))
            if (from_id, to_id) == (LOCAL_NODE_ID, "node-a"):
                await gate.wait()
            return {}

        monkeypatch.setattr(registry, "probe_edge", fake_probe_edge)

        first = await registry.probe_links([[LOCAL_NODE_ID, "node-a"]])
        assert first["total_edges"] == 1
        first_task = registry._probe_tasks[first["probe_id"]]

        # 在途（首条边阻塞在 gate 上）时请求不相交的边集
        second = await registry.probe_links([["node-a", "node-b"]])
        assert second["probe_id"] != first["probe_id"]
        assert second["total_edges"] == 1
        await asyncio.wait_for(registry._probe_tasks[second["probe_id"]], timeout=5)
        assert ("node-a", "node-b") in probed

        # 子集请求仍走防抖：返回在途 probe_id，不重复探测
        third = await registry.probe_links([[LOCAL_NODE_ID, "node-a"]])
        assert third["probe_id"] in {first["probe_id"], second["probe_id"]}

        gate.set()
        await asyncio.wait_for(first_task, timeout=5)
        assert probed.count((LOCAL_NODE_ID, "node-a")) == 1
        assert registry._probing is False

    asyncio.run(scenario())


# ---------- 7. 临时 ssh-agent 回落目录与异常回收 ----------


def make_deep_registry(tmp_path, monkeypatch) -> NodeRegistryService:
    """state_dir 足够深，使 run 目录下的 sock 路径超长，强制走 /tmp 回落分支。"""
    bindir = install_fake_bins(tmp_path)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    database = Database(tmp_path / "scheduler.db")
    database.init()
    return NodeRegistryService(
        database=database,
        events=EventBroker(),
        state_dir=tmp_path / ("d" * 120) / "state",
        server_name=SERVER_NAME,
        ssh_binary=str(bindir / "ssh"),
        ssh_keygen_binary=str(bindir / "ssh-keygen"),
        sshpass_binary=str(bindir / "sshpass"),
    )


def test_temp_agent_fallback_dir_rejects_insecure(tmp_path, monkeypatch):
    """回落目录已存在但权限不为 0700：拒绝使用（防公共 /tmp 抢注）。"""
    fake_tmp = tmp_path / "fake-tmp"
    fake_tmp.mkdir()
    monkeypatch.setattr(
        "exp_scheduler_app.nodes.tempfile.gettempdir", lambda: str(fake_tmp)
    )
    registry = make_deep_registry(tmp_path, monkeypatch)
    bad_dir = fake_tmp / f"exp-sched-{os.getuid()}"
    bad_dir.mkdir()
    os.chmod(bad_dir, 0o755)

    with pytest.raises(ValueError, match="回落目录不可信"):
        asyncio.run(registry._start_temp_agent("probe"))


def test_temp_agent_fallback_dir_created_secure(tmp_path, monkeypatch):
    """回落目录不存在：以 0700 创建，agent sock 落在其中。"""
    fake_tmp = tmp_path / "fake-tmp"
    fake_tmp.mkdir()
    monkeypatch.setattr(
        "exp_scheduler_app.nodes.tempfile.gettempdir", lambda: str(fake_tmp)
    )
    registry = make_deep_registry(tmp_path, monkeypatch)

    agent = asyncio.run(registry._start_temp_agent("probe"))
    fallback = fake_tmp / f"exp-sched-{os.getuid()}"
    assert fallback.is_dir()
    assert (fallback.stat().st_mode & 0o777) == 0o700
    assert agent.sock.startswith(str(fallback))
    registry._stop_temp_agent(agent)
    # _stop_temp_agent 回收 sock 占位文件
    assert not Path(agent.sock).exists()


def test_temp_agent_parse_failure_cleans_socket(tmp_path, monkeypatch):
    """ssh-agent 输出无法解析 PID 时抛中文错误，且 sock 占位文件被回收。"""
    registry, _database = make_registry(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_AGENT_OUTPUT", "garbage")

    with pytest.raises(ValueError, match="无法解析临时 ssh-agent"):
        asyncio.run(registry._start_temp_agent("probe"))
    run_dir = tmp_path / "state" / "run"
    assert not list(run_dir.glob("*.sock"))


def test_probe_hop_ssh_add_failure_stops_temp_agent(tmp_path, monkeypatch):
    """hop 探测中 ssh-add 加载密钥失败：异常路径必经 _stop_temp_agent（sock 被回收）。"""
    registry, database = make_registry(tmp_path, monkeypatch)
    add_key_node(database, tmp_path, "node-a", "node-a")
    add_key_node(database, tmp_path, "node-b", "node-b")
    monkeypatch.setenv("FAKE_SSH_ADD_FAIL", "1")

    with pytest.raises(ValueError, match="加载节点"):
        asyncio.run(registry.probe_edge("node-a", "node-b"))
    run_dir = tmp_path / "state" / "run"
    assert not list(run_dir.glob("*.sock"))


# ---------- 8. 纯函数：build_ssh_command / host_key_alias ----------


def test_build_ssh_command_key_auth(tmp_path):
    auth = ResolvedAuth(
        node_id="n1",
        name="节点1",
        is_local=False,
        host="10.0.0.8",
        port=2222,
        username="ubuntu",
        auth_method="key",
        key_path="/keys/k1",
        password=None,
    )
    argv, env_extra = build_ssh_command(
        auth,
        known_hosts_path=tmp_path / "known_hosts",
        remote_command="echo hi",
        forward_agent=True,
    )
    assert argv[0] == "ssh"
    assert env_extra == {}
    assert "-A" in argv
    key_index = argv.index("-i")
    assert argv[key_index + 1] == "/keys/k1"
    assert "IdentitiesOnly=yes" in argv
    assert "BatchMode=yes" in argv
    port_index = argv.index("-p")
    assert argv[port_index + 1] == "2222"
    assert f"UserKnownHostsFile={tmp_path / 'known_hosts'}" in argv
    # 主机与远端命令位于 -- 之后，防止选项注入
    assert argv[-3:] == ["--", "ubuntu@10.0.0.8", "echo hi"]


def test_build_ssh_command_password_auth(tmp_path):
    password = "s3cret-pw"
    auth = ResolvedAuth(
        node_id="n2",
        name="节点2",
        is_local=False,
        host="10.0.0.9",
        port=22,
        username="ubuntu",
        auth_method="password",
        key_path=None,
        password=password,
    )
    argv, env_extra = build_ssh_command(
        auth,
        known_hosts_path=tmp_path / "known_hosts",
        remote_command="true",
    )
    # 密码经 sshpass -e + 环境变量传递，绝不进 argv
    assert argv[0] == "sshpass"
    assert argv[1] == "-e"
    assert env_extra == {"SSHPASS": password}
    assert all(password not in part for part in argv)
    assert "PreferredAuthentications=password,keyboard-interactive" in argv
    assert "PubkeyAuthentication=no" in argv
    assert "NumberOfPasswordPrompts=1" in argv
    assert "BatchMode=yes" not in argv
    assert argv[-3:] == ["--", "ubuntu@10.0.0.9", "true"]


def test_host_key_alias_and_option_args(tmp_path):
    assert host_key_alias("abc123") == "expsched-abc123"
    options = build_ssh_option_args(port=2200, known_hosts_path=tmp_path / "kh")
    assert options[:2] == ["-p", "2200"]
    assert f"UserKnownHostsFile={tmp_path / 'kh'}" in options
    assert "StrictHostKeyChecking=accept-new" in options
