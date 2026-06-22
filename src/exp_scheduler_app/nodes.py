"""节点注册表与 SSH 密钥库服务。

职责：
- SSH 密钥库：托管粘贴的私钥（state_dir/keys，0600）或引用外部路径，DB 永不存私钥内容
- 节点 CRUD：local 伪节点注入、密码字段脱敏（password → has_password）
- 测试连接：基础探测 + 端口转发探针 + agent 转发探针，结果写 node_links 与节点能力列
- 连通性矩阵探测：local→X 直连探测、A→B 经主控 hop 探测（临时 ssh-agent + agent 转发）
- 纯函数 build_ssh_option_args / build_ssh_command / host_key_alias 供
  transfer.py、interactive_terminal.py、conda_inventory.py 复用

安全约定：
- 子进程一律 argv 列表 + start_new_session=True，不经 shell
- 密码只经环境变量 SSHPASS 传给 sshpass，绝不进 argv / 日志 / API 响应
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import signal
import sqlite3
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .database import LOCAL_NODE_ID, Database
from .events import EventBroker

LOGGER = logging.getLogger("exp_scheduler")

# ---------- 探测超时与并发常量 ----------
BASIC_PROBE_TIMEOUT_SECONDS = 20.0
FORWARD_PROBE_TIMEOUT_SECONDS = 12.0
AGENT_PROBE_TIMEOUT_SECONDS = 12.0
HOP_PROBE_TIMEOUT_SECONDS = 25.0
LOCAL_COMMAND_TIMEOUT_SECONDS = 10.0
PROBE_CONCURRENCY = 4

# ---------- stdout 标记（探测结果判读靠标记而非 exit code） ----------
BASIC_OK_MARKER = "EXP_SCHED_OK"
HAS_SSHPASS_MARKER = "HAS_SSHPASS"
HOP_OK_MARKER = "EXP_SCHED_HOP_OK"
LINK_OK_MARKER = "EXP_SCHED_LINK_OK"
LINK_FAIL_PATTERN = re.compile(r"EXP_SCHED_LINK_FAIL rc=(\d+)")
AGENT_RC_PATTERN = re.compile(r"RC=(\d+)")
SSH_AGENT_PID_PATTERN = re.compile(r"SSH_AGENT_PID=(\d+)")

# 远端发起端上对目标节点使用的独立 known_hosts（TOFU，不污染用户文件）
REMOTE_KNOWN_HOSTS_PATH = "$HOME/.exp-scheduler.known_hosts"

# 基础探测脚本：一次 SSH 同时产出连通性 + rsync 版本 + sshpass 能力
BASIC_PROBE_SCRIPT = (
    "echo EXP_SCHED_OK; "
    "rsync --version 2>/dev/null | head -n1; "
    "command -v sshpass >/dev/null 2>&1 && echo HAS_SSHPASS"
)
# agent 转发探针：判据用 ssh-add 退出码（2=连不上 agent），
# 而非 SSH_AUTH_SOCK 是否存在——防远端 .bashrc 假导出造成漏报
AGENT_PROBE_SCRIPT = "ssh-add -l >/dev/null 2>&1; echo RC=$?"

# host/username 校验正则（防 -oProxyCommand= 等选项注入）
HOST_PATTERN = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")
USERNAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_\-]*$")

# Unix socket 路径长度上限（sun_path 108 字节，留余量）
_MAX_SOCKET_PATH_BYTES = 100


@dataclass(slots=True)
class ResolvedAuth:
    """节点连接参数的解析结果（'local' 时 is_local=True，其余字段为 None/0）。"""

    node_id: str
    name: str
    is_local: bool
    host: str | None
    port: int
    username: str | None
    auth_method: str | None
    key_path: str | None
    password: str | None


def host_key_alias(node_id: str) -> str:
    """known_hosts 中 peer 节点的稳定别名（HostKeyAlias 按字面匹配，不加端口装饰）。"""
    return f"expsched-{node_id}"


def build_ssh_option_args(
    *,
    port: int,
    known_hosts_path: Path | str,
    connect_timeout: int = 10,
    server_alive_interval: int = 15,
    server_alive_count_max: int = 6,
    strict_host_key_checking: str = "accept-new",
) -> list[str]:
    """通用 SSH 选项（不含认证相关选项，认证由 build_ssh_command 追加）。"""
    return [
        "-p",
        str(port),
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        f"ServerAliveInterval={server_alive_interval}",
        "-o",
        f"ServerAliveCountMax={server_alive_count_max}",
        "-o",
        f"UserKnownHostsFile={known_hosts_path}",
        "-o",
        f"StrictHostKeyChecking={strict_host_key_checking}",
    ]


def build_ssh_command(
    auth: ResolvedAuth,
    *,
    known_hosts_path: Path | str,
    remote_command: str | None = None,
    forward_agent: bool = False,
    extra_options: list[str] | None = None,
    ssh_binary: str = "ssh",
    sshpass_binary: str = "sshpass",
) -> tuple[list[str], dict[str, str]]:
    """构造主控发起的 SSH 命令，返回 (argv, env_extra)。

    - 密码认证：argv 前缀 [sshpass, "-e"]，密码经 env_extra["SSHPASS"] 传递；
      加 PreferredAuthentications/PubkeyAuthentication=no/NumberOfPasswordPrompts=1，
      不加 BatchMode（sshpass 需要密码提示）。
    - 密钥认证：-i path -o IdentitiesOnly=yes -o BatchMode=yes。
    """
    if auth.is_local:
        raise ValueError("本机节点无需构造 SSH 命令")
    if not auth.host or not auth.username:
        raise ValueError(f"节点 {auth.name} 缺少主机或用户名配置")

    argv: list[str] = []
    env_extra: dict[str, str] = {}
    if auth.auth_method == "password":
        if not auth.password:
            raise ValueError(f"节点 {auth.name} 未配置密码")
        argv += [sshpass_binary, "-e"]
        env_extra["SSHPASS"] = auth.password

    argv.append(ssh_binary)
    if forward_agent:
        argv.append("-A")
    argv += build_ssh_option_args(port=auth.port, known_hosts_path=known_hosts_path)

    if auth.auth_method == "password":
        argv += [
            "-o",
            "PreferredAuthentications=password,keyboard-interactive",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=1",
        ]
    elif auth.auth_method == "key":
        if not auth.key_path:
            raise ValueError(f"节点 {auth.name} 未配置 SSH 密钥")
        argv += ["-i", auth.key_path, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
    else:
        raise ValueError(f"节点 {auth.name} 的认证方式无效: {auth.auth_method}")

    if extra_options:
        argv += extra_options
    argv += ["--", f"{auth.username}@{auth.host}"]
    if remote_command is not None:
        argv.append(remote_command)
    return argv, env_extra


def translate_ssh_error(stderr: str, *, fallback: str = "SSH 连接失败") -> str:
    """把 ssh 常见 stderr 映射为中文提示，并附原文尾行便于排障。"""
    tail = ""
    for line in reversed(stderr.strip().splitlines()):
        stripped = line.strip()
        if stripped:
            tail = stripped[-200:]
            break
    lowered = stderr.lower()
    if "permission denied" in lowered or "too many authentication failures" in lowered:
        message = "认证失败"
    elif "connection refused" in lowered:
        message = "网络不可达或 sshd 未运行"
    elif "timed out" in lowered:
        message = "网络不可达或连接超时"
    elif "host key verification" in lowered or "identification has changed" in lowered:
        message = "主机密钥变更或校验失败"
    elif "no route" in lowered:
        message = "路由不可达"
    elif "could not resolve hostname" in lowered:
        message = "主机名无法解析"
    else:
        return tail or fallback
    return f"{message}（{tail}）" if tail else message


def _truncate(text: str, limit: int = 200) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _snapshot_mentions(snapshot: object, node_id: str) -> bool:
    """递归检查传输任务的节点快照中是否引用了指定节点。"""
    if isinstance(snapshot, dict):
        if snapshot.get("id") == node_id or snapshot.get("node_id") == node_id:
            return True
        return any(_snapshot_mentions(value, node_id) for value in snapshot.values())
    if isinstance(snapshot, list):
        return any(_snapshot_mentions(item, node_id) for item in snapshot)
    return False


@dataclass(slots=True)
class _ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool


@dataclass(slots=True)
class _TempAgent:
    pid: int
    sock: str


@dataclass(slots=True)
class _BasicProbeResult:
    ok: bool
    rsync_version: str | None
    has_sshpass: bool
    latency_ms: float | None
    detail: str


class NodeRegistryService:
    """节点注册表 + SSH 密钥库 + 连通性探测（参照 NvitopTerminalService 的独立 service 风格）。"""

    def __init__(
        self,
        *,
        database: Database,
        events: EventBroker,
        state_dir: Path,
        server_name: str,
        ssh_binary: str = "ssh",
        ssh_keygen_binary: str = "ssh-keygen",
        sshpass_binary: str = "sshpass",
    ) -> None:
        self.database = database
        self.events = events
        self.state_dir = Path(state_dir)
        self.server_name = server_name
        self.ssh_binary = ssh_binary
        self.ssh_keygen_binary = ssh_keygen_binary
        self.sshpass_binary = sshpass_binary
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._probe_guard = asyncio.Lock()
        # 在途 sweep 任务与各自负责的边集（probe_id → task / edges），支持并行 sweep
        self._probe_tasks: dict[str, asyncio.Task[None]] = {}
        self._probe_edges: dict[str, set[tuple[str, str]]] = {}
        self._probe_response: dict[str, object] | None = None

    # ---------- SSH 密钥库 ----------

    async def list_keys(self) -> list[dict[str, object]]:
        # DB 行只含路径/公钥/指纹，无私钥内容，可直接输出
        return self.database.list_ssh_keys()

    async def create_key(
        self,
        *,
        name: str,
        private_key: str | None = None,
        private_key_path: str | None = None,
        notes: str | None = None,
    ) -> dict[str, object]:
        name = (name or "").strip()
        if not name:
            raise ValueError("密钥名称不能为空")
        if (private_key is None) == (private_key_path is None):
            raise ValueError("private_key（粘贴内容）与 private_key_path（引用路径）必须二选一")

        if private_key is not None:
            kind = "managed"
            key_path = self._write_managed_key(private_key)
            try:
                public_key, fingerprint = await self._inspect_private_key(
                    str(key_path), strict=True
                )
            except ValueError:
                with contextlib.suppress(OSError):
                    key_path.unlink()
                raise
        else:
            kind = "external"
            path = Path(str(private_key_path)).expanduser()
            if not path.is_file() or not os.access(path, os.R_OK):
                # 措辞避开"不存在"：这是请求参数错误（400），而非实体未找到（404）
                raise ValueError(f"无法读取私钥文件（路径无效或权限不足）: {path}")
            key_path = path
            # external 引用只查存在可读，公钥/指纹尽力提取
            public_key, fingerprint = await self._inspect_private_key(str(path), strict=False)

        key_id = uuid4().hex
        try:
            key = self.database.create_ssh_key(
                key_id=key_id,
                name=name,
                kind=kind,
                key_path=str(key_path),
                public_key=public_key,
                fingerprint=fingerprint,
                notes=(notes or None),
            )
        except sqlite3.IntegrityError as exc:
            if kind == "managed":
                with contextlib.suppress(OSError):
                    key_path.unlink()
            raise ValueError(f"密钥名称已存在: {name}") from exc

        await self._record_operation(
            action="ssh_key_created",
            entity_type="ssh_key",
            title=f"创建 SSH 密钥: {name}",
            metadata={"key_id": key_id, "kind": kind, "fingerprint": fingerprint},
        )
        await self.events.publish("ssh_keys_updated", {"key_id": key_id, "action": "created"})
        return key

    async def update_key(
        self,
        key_id: str,
        *,
        name: str | None = None,
        notes: str | None = None,
    ) -> dict[str, object]:
        key = self.database.get_ssh_key(key_id)
        if key is None:
            raise ValueError(f"SSH 密钥不存在: {key_id}")
        if name is not None:
            new_name = name.strip()
            if not new_name:
                raise ValueError("密钥名称不能为空")
        else:
            new_name = str(key["name"])
        # notes=None 表示不变，空串表示清除
        new_notes = key["notes"] if notes is None else (notes.strip() or None)
        try:
            updated = self.database.update_ssh_key(key_id, name=new_name, notes=new_notes)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"密钥名称已存在: {new_name}") from exc
        await self._record_operation(
            action="ssh_key_updated",
            entity_type="ssh_key",
            title=f"更新 SSH 密钥: {new_name}",
            metadata={"key_id": key_id},
        )
        await self.events.publish("ssh_keys_updated", {"key_id": key_id, "action": "updated"})
        return updated

    async def delete_key(self, key_id: str) -> None:
        key = self.database.get_ssh_key(key_id)
        if key is None:
            raise ValueError(f"SSH 密钥不存在: {key_id}")
        used = self.database.count_nodes_using_key(key_id)
        if used > 0:
            raise ValueError(f"密钥正被 {used} 个节点使用，请先修改这些节点的认证配置再删除")
        self.database.delete_ssh_key(key_id)
        if key["kind"] == "managed":
            with contextlib.suppress(OSError):
                Path(str(key["key_path"])).unlink()
        await self._record_operation(
            action="ssh_key_deleted",
            entity_type="ssh_key",
            title=f"删除 SSH 密钥: {key['name']}",
            metadata={"key_id": key_id, "kind": key["kind"]},
        )
        await self.events.publish("ssh_keys_updated", {"key_id": key_id, "action": "deleted"})

    # ---------- 节点 CRUD ----------

    async def list_nodes(self) -> list[dict[str, object]]:
        payload: list[dict[str, object]] = [self._local_node_payload()]
        payload.extend(self._sanitize_node(node) for node in self.database.list_nodes())
        return payload

    async def get_node_payload(self, node_id: str) -> dict[str, object]:
        if node_id == LOCAL_NODE_ID:
            return self._local_node_payload()
        node = self.database.get_node(node_id)
        if node is None:
            raise ValueError(f"节点不存在: {node_id}")
        return self._sanitize_node(node)

    async def create_node(
        self,
        *,
        name: str,
        host: str,
        username: str,
        auth_method: str,
        ssh_port: int = 22,
        ssh_key_id: str | None = None,
        password: str | None = None,
        notes: str | None = None,
    ) -> dict[str, object]:
        fields = self._validate_node_fields(
            name=name,
            host=host,
            ssh_port=ssh_port,
            username=username,
            auth_method=auth_method,
            ssh_key_id=ssh_key_id,
            password=password,
        )
        node_id = uuid4().hex
        try:
            node = self.database.create_node(node_id=node_id, notes=(notes or None), **fields)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"节点名称已存在: {fields['name']}") from exc
        await self._record_operation(
            action="node_created",
            entity_type="node",
            title=f"注册节点: {fields['name']}",
            metadata={
                "node_id": node_id,
                "host": fields["host"],
                "ssh_port": fields["ssh_port"],
                "auth_method": fields["auth_method"],
            },
        )
        await self.events.publish("node_updated", {"node_id": node_id, "action": "created"})
        return self._sanitize_node(node)

    async def update_node(
        self,
        node_id: str,
        *,
        name: str | None = None,
        host: str | None = None,
        ssh_port: int | None = None,
        username: str | None = None,
        auth_method: str | None = None,
        ssh_key_id: str | None = None,
        password: str | None = None,
        notes: str | None = None,
    ) -> dict[str, object]:
        existing = self.database.get_node(node_id)
        if existing is None:
            raise ValueError(f"节点不存在: {node_id}")
        # password=None 表示不变（沿用旧值），空串表示清除
        merged_password = existing["password"] if password is None else (password or None)
        fields = self._validate_node_fields(
            name=name if name is not None else str(existing["name"]),
            host=host if host is not None else str(existing["host"]),
            ssh_port=ssh_port if ssh_port is not None else int(existing["ssh_port"]),  # type: ignore[arg-type]
            username=username if username is not None else str(existing["username"]),
            auth_method=auth_method if auth_method is not None else str(existing["auth_method"]),
            ssh_key_id=ssh_key_id if ssh_key_id is not None else existing["ssh_key_id"],
            password=merged_password,  # type: ignore[arg-type]
        )
        merged_notes = existing["notes"] if notes is None else (notes.strip() or None)
        try:
            node = self.database.update_node(node_id, notes=merged_notes, **fields)  # type: ignore[arg-type]
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"节点名称已存在: {fields['name']}") from exc
        await self._record_operation(
            action="node_updated",
            entity_type="node",
            title=f"更新节点: {fields['name']}",
            metadata={"node_id": node_id, "auth_method": fields["auth_method"]},
        )
        await self.events.publish("node_updated", {"node_id": node_id, "action": "updated"})
        return self._sanitize_node(node)

    async def delete_node(self, node_id: str) -> None:
        node = self.database.get_node(node_id)
        if node is None:
            raise ValueError(f"节点不存在: {node_id}")
        for job in self.database.list_active_transfer_jobs():
            if (
                job.get("src_node_id") == node_id
                or job.get("dst_node_id") == node_id
                or _snapshot_mentions(job.get("node_snapshot"), node_id)
            ):
                raise ValueError(
                    f"节点 {node['name']} 正被运行中的传输任务使用，无法删除（任务 {job.get('id')}）"
                )
        self.database.delete_node(node_id)
        await self._record_operation(
            action="node_deleted",
            entity_type="node",
            title=f"删除节点: {node['name']}",
            metadata={"node_id": node_id, "host": node["host"]},
        )
        await self.events.publish("node_updated", {"node_id": node_id, "action": "deleted"})

    # ---------- 认证解析（同步，供 transfer / interactive_terminal 调用） ----------

    def resolve_auth(self, node_id: str) -> ResolvedAuth:
        if node_id == LOCAL_NODE_ID:
            return ResolvedAuth(
                node_id=LOCAL_NODE_ID,
                name=self.server_name,
                is_local=True,
                host=None,
                port=0,
                username=None,
                auth_method=None,
                key_path=None,
                password=None,
            )
        node = self.database.get_node(node_id)
        if node is None:
            raise ValueError(f"节点不存在: {node_id}")
        key_path: str | None = None
        if node["auth_method"] == "key":
            ssh_key_id = node["ssh_key_id"]
            key = self.database.get_ssh_key(str(ssh_key_id)) if ssh_key_id else None
            if key is None:
                raise ValueError(f"节点 {node['name']} 引用的 SSH 密钥不存在")
            key_path = str(key["key_path"])
        return ResolvedAuth(
            node_id=str(node["id"]),
            name=str(node["name"]),
            is_local=False,
            host=str(node["host"]),
            port=int(node["ssh_port"]),  # type: ignore[arg-type]
            username=str(node["username"]),
            auth_method=str(node["auth_method"]),
            key_path=key_path,
            password=node["password"] if node["password"] else None,  # type: ignore[arg-type]
        )

    def known_hosts_path(self) -> Path:
        # 文件不存在时由 ssh 在 accept-new 首连时自建
        return self.state_dir / "known_hosts"

    def lookup_host_key_lines(self, host: str, port: int, alias: str) -> list[str]:
        """从主控 known_hosts 提取 host 的密钥行并将主机列替换为别名（多算法多行）。

        known_hosts 的主机匹配语义本就大小写不敏感（OpenSSH 记录前会把主机名小写化），
        故 tokens 与行内 hosts 字段双侧统一小写后比较，节点 host 含大写也能命中。
        """
        path = self.known_hosts_path()
        if not path.exists():
            return []
        host_lower = host.lower()
        tokens = {f"[{host_lower}]:{port}"}
        if port == 22:
            tokens.add(host_lower)
        lines: list[str] = []
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for raw in content.splitlines():
            line = raw.strip()
            # 跳过空行、注释、@marker 行与 hash 行（我们创建的文件从不开 HashKnownHosts）
            if not line or line.startswith("#") or line.startswith("@") or line.startswith("|"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            hosts_field, keytype, key_b64 = parts[0], parts[1], parts[2]
            if tokens & {item.lower() for item in hosts_field.split(",")}:
                lines.append(f"{alias} {keytype} {key_b64}")
        return lines

    # ---------- 测试连接 ----------

    async def test_node(self, node_id: str) -> dict[str, object]:
        """三连探测：基础连通 + 端口转发探针 + agent 转发探针（总计 ~45s 内）。"""
        auth = self.resolve_auth(node_id)
        if auth.is_local:
            raise ValueError("本机节点无需测试连接")

        link, basic = await self._probe_direct(node_id, write_capabilities=False)
        del link  # 已 upsert 并广播，详情统一从返回值取
        if not basic.ok:
            await self._record_operation(
                level="warning",
                action="node_tested",
                entity_type="node",
                title=f"测试节点连接失败: {auth.name}",
                detail=basic.detail,
                metadata={"node_id": node_id, "ok": False},
            )
            return {
                "ok": False,
                "rsync_version": None,
                "has_sshpass": None,
                "tcp_forward_ok": None,
                "agent_forward_ok": None,
                "latency_ms": None,
                "detail": basic.detail,
            }

        tcp_forward_ok = await self._probe_tcp_forward(auth)
        agent_forward_ok = await self._probe_agent_forward(auth)
        self.database.update_node_capabilities(
            node_id,
            rsync_version=basic.rsync_version,
            has_sshpass=basic.has_sshpass,
            tcp_forward_ok=tcp_forward_ok,
            agent_forward_ok=agent_forward_ok,
        )
        await self.events.publish("node_updated", {"node_id": node_id, "action": "capabilities"})

        detail_parts = ["连接成功"]
        if not basic.rsync_version:
            detail_parts.append("未检测到 rsync（文件同步不可用）")
        if tcp_forward_ok is False:
            detail_parts.append("sshd 拒绝端口转发（无法作为桥接发起端）")
        if agent_forward_ok is False:
            detail_parts.append("sshd 禁用 agent 转发（无法作为远程发起端向对端认证）")
        detail = "；".join(detail_parts)
        await self._record_operation(
            action="node_tested",
            entity_type="node",
            title=f"测试节点连接: {auth.name}",
            detail=detail,
            metadata={
                "node_id": node_id,
                "ok": True,
                "rsync_version": basic.rsync_version,
                "has_sshpass": basic.has_sshpass,
                "tcp_forward_ok": tcp_forward_ok,
                "agent_forward_ok": agent_forward_ok,
            },
        )
        return {
            "ok": True,
            "rsync_version": basic.rsync_version,
            "has_sshpass": basic.has_sshpass,
            "tcp_forward_ok": tcp_forward_ok,
            "agent_forward_ok": agent_forward_ok,
            "latency_ms": basic.latency_ms,
            "detail": detail,
        }

    # ---------- 连通性矩阵 ----------

    async def probe_edge(
        self,
        from_node_id: str,
        to_node_id: str,
        *,
        record_operation: bool = True,
    ) -> dict[str, object]:
        """单边同步探测，返回 from→to 的 link dict（写库 + SSE 推送）。"""
        if to_node_id == LOCAL_NODE_ID:
            raise ValueError("不支持探测指向本机的连通边")
        if from_node_id == to_node_id:
            raise ValueError("探测的起点与终点不能相同")
        if from_node_id == LOCAL_NODE_ID:
            link, _basic = await self._probe_direct(to_node_id)
        else:
            link = await self._probe_hop(from_node_id, to_node_id)
        if record_operation:
            await self._record_operation(
                action="node_link_probed",
                entity_type="node_link",
                title=f"探测连通边: {from_node_id} → {to_node_id}",
                detail=str(link.get("last_error") or "连通"),
                metadata={
                    "from_node_id": from_node_id,
                    "to_node_id": to_node_id,
                    "status": link.get("status"),
                },
            )
        return link

    async def probe_links(
        self,
        pairs: Sequence[Sequence[str]] | None = None,
    ) -> dict[str, object]:
        """启动后台批量探测（202 语义）。pairs=None 时探测全矩阵。

        防抖仅在「请求边集 ⊆ 在途边集」时返回在途探测的 probe_id；
        否则为不在途的差集边另起 sweep，避免不同 pairs 的请求被静默吞掉。
        """
        async with self._probe_guard:
            # 清理已结束但未自清的任务记录（如启动前即被取消的 task）
            for stale_id in [pid for pid, task in self._probe_tasks.items() if task.done()]:
                self._probe_tasks.pop(stale_id, None)
                self._probe_edges.pop(stale_id, None)
            edges, skipped = self._collect_probe_edges(pairs)
            inflight = self._inflight_edges()
            if inflight:
                if set(edges) <= inflight:
                    # 请求边集已全部在途：返回同一 probe_id（防抖）
                    return dict(self._probe_response or {})
                # 仅为不在途的差集边起新 sweep，在途部分由已有 sweep 覆盖
                edges = [edge for edge in edges if edge not in inflight]
            probe_id = uuid4().hex
            response: dict[str, object] = {
                "probe_id": probe_id,
                "total_edges": len(edges),
                "skipped": skipped,
            }
            self._probe_response = response
            await self._record_operation(
                action="probe_links_started",
                entity_type="node_link",
                title=f"开始连通性探测（{len(edges)} 条边）",
                metadata={
                    "probe_id": probe_id,
                    "total_edges": len(edges),
                    "skipped_count": len(skipped),
                },
            )
            await self.events.publish(
                "node_links_probe_started",
                {"probe_id": probe_id, "total_edges": len(edges)},
            )
            if edges:
                self._probe_edges[probe_id] = set(edges)
                self._probe_tasks[probe_id] = asyncio.create_task(
                    self._run_probe_sweep(probe_id, edges),
                    name=f"node-links-probe-{probe_id[:8]}",
                )
            else:
                await self.events.publish("node_links_probe_finished", {"probe_id": probe_id})
            return dict(response)

    async def links_payload(self) -> dict[str, object]:
        """连通性矩阵快照 + applicable 动态字段 + probing 状态。"""
        nodes = {str(node["id"]): node for node in self.database.list_nodes()}
        links: list[dict[str, object]] = []
        for link in self.database.list_node_links():
            to_node = nodes.get(str(link["to_node_id"]))
            applicable = True
            if (
                str(link["from_node_id"]) != LOCAL_NODE_ID
                and to_node is not None
                and to_node["auth_method"] == "password"
            ):
                # 不变式：由远端连入的目标必须密钥认证
                applicable = False
            link["applicable"] = applicable
            links.append(link)
        return {"links": links, "probing": self._probing}

    # ---------- 内部：节点校验与脱敏 ----------

    def _local_node_payload(self) -> dict[str, object]:
        return {"id": LOCAL_NODE_ID, "name": self.server_name, "is_local": True}

    def _sanitize_node(self, node: dict[str, object]) -> dict[str, object]:
        payload = dict(node)
        password = payload.pop("password", None)
        payload["has_password"] = bool(password)
        payload["is_local"] = False
        return payload

    def _validate_node_fields(
        self,
        *,
        name: str,
        host: str,
        ssh_port: int,
        username: str,
        auth_method: str,
        ssh_key_id: object,
        password: str | None,
    ) -> dict[str, object]:
        name = (name or "").strip()
        if not name:
            raise ValueError("节点名称不能为空")
        if name.lower() == LOCAL_NODE_ID:
            raise ValueError("节点名称不能为 local（保留给本机伪节点）")
        host = (host or "").strip()
        if not host or not HOST_PATTERN.match(host):
            raise ValueError(f"主机地址格式无效: {host or '(空)'}")
        try:
            port = int(ssh_port)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"SSH 端口无效: {ssh_port}") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"SSH 端口必须在 1-65535 之间: {port}")
        username = (username or "").strip()
        if not username or not USERNAME_PATTERN.match(username):
            raise ValueError(f"用户名格式无效: {username or '(空)'}")
        if auth_method not in ("key", "password"):
            raise ValueError(f"认证方式无效: {auth_method}（仅支持 key / password）")
        key_id_value = str(ssh_key_id) if ssh_key_id else None
        if auth_method == "key":
            if not key_id_value:
                raise ValueError("密钥认证方式必须指定 ssh_key_id")
            if self.database.get_ssh_key(key_id_value) is None:
                raise ValueError(f"SSH 密钥不存在: {key_id_value}")
            # 密钥认证不保留明文密码（与下方清空 ssh_key_id 对称），
            # 否则 password→key 切换后旧口令长期驻留 DB，且 has_password 误报
            password = None
        else:
            if not password:
                raise ValueError("密码认证方式必须提供密码")
            # 密码认证不保留密钥引用，否则切换认证方式后会一直阻塞该密钥的删除
            key_id_value = None
        return {
            "name": name,
            "host": host,
            "ssh_port": port,
            "username": username,
            "auth_method": auth_method,
            "ssh_key_id": key_id_value,
            "password": password or None,
        }

    # ---------- 内部：密钥文件管理 ----------

    def _write_managed_key(self, private_key: str) -> Path:
        keys_dir = self.state_dir / "keys"
        keys_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(keys_dir, 0o700)
        key_path = keys_dir / uuid4().hex
        content = private_key.replace("\r\n", "\n").strip("\n") + "\n"
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
        except Exception:
            with contextlib.suppress(OSError):
                key_path.unlink()
            raise
        # 回读校验：NTFS/drvfs 上 chmod 0600 无效，ssh 会拒绝该密钥
        mode = os.stat(key_path).st_mode & 0o777
        if mode != 0o600:
            with contextlib.suppress(OSError):
                key_path.unlink()
            raise ValueError(
                "无法将私钥文件权限设为 0600：state_dir 所在文件系统不支持 POSIX 权限"
                "（NTFS/drvfs）。WSL 下请将 state_dir 移出 /mnt/c 等挂载路径。"
            )
        return key_path

    async def _inspect_private_key(
        self,
        key_path: str,
        *,
        strict: bool,
    ) -> tuple[str | None, str | None]:
        """ssh-keygen -y 验证私钥并取公钥首行；-lf 取指纹。strict 时验证失败抛 ValueError。"""
        result = await self._run_process(
            [self.ssh_keygen_binary, "-y", "-f", key_path],
            timeout_seconds=LOCAL_COMMAND_TIMEOUT_SECONDS,
        )
        public_key: str | None = None
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.strip():
                    public_key = line.strip()
                    break
        elif strict:
            detail = _truncate(result.stderr.strip() or result.stdout.strip())
            raise ValueError(
                "私钥校验失败：内容无效或受 passphrase 保护（不支持带 passphrase 的密钥，"
                "可先用 ssh-keygen -p -N '' 去除后再粘贴）。"
                + (f" 详情: {detail}" if detail else "")
            )
        fingerprint: str | None = None
        fp_result = await self._run_process(
            [self.ssh_keygen_binary, "-lf", key_path],
            timeout_seconds=LOCAL_COMMAND_TIMEOUT_SECONDS,
        )
        if fp_result.returncode == 0:
            fp_lines = [line.strip() for line in fp_result.stdout.splitlines() if line.strip()]
            if fp_lines:
                fingerprint = fp_lines[0]
        return public_key, fingerprint

    # ---------- 内部：探测原语 ----------

    async def _basic_probe(self, auth: ResolvedAuth) -> _BasicProbeResult:
        remote_command = "sh -c " + shlex.quote(BASIC_PROBE_SCRIPT)
        argv, env_extra = build_ssh_command(
            auth,
            known_hosts_path=self.known_hosts_path(),
            remote_command=remote_command,
            ssh_binary=self.ssh_binary,
            sshpass_binary=self.sshpass_binary,
        )
        result = await self._run_process(
            argv, env_extra=env_extra, timeout_seconds=BASIC_PROBE_TIMEOUT_SECONDS
        )
        if result.timed_out:
            return _BasicProbeResult(
                ok=False,
                rsync_version=None,
                has_sshpass=False,
                latency_ms=None,
                detail=f"连接超时（{int(BASIC_PROBE_TIMEOUT_SECONDS)} 秒）",
            )
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if BASIC_OK_MARKER not in lines:
            return _BasicProbeResult(
                ok=False,
                rsync_version=None,
                has_sshpass=False,
                latency_ms=None,
                detail=translate_ssh_error(result.stderr),
            )
        rsync_version = next((line for line in lines if line.startswith("rsync")), None)
        return _BasicProbeResult(
            ok=True,
            rsync_version=rsync_version,
            has_sshpass=HAS_SSHPASS_MARKER in lines,
            latency_ms=result.duration_ms,
            detail="连接成功",
        )

    async def _probe_direct(
        self,
        node_id: str,
        *,
        write_capabilities: bool = True,
    ) -> tuple[dict[str, object], _BasicProbeResult]:
        """local→X 直连探测：连通结果写 node_links，能力顺手写节点能力列。"""
        auth = self.resolve_auth(node_id)
        basic = await self._basic_probe(auth)
        if basic.ok:
            link = self.database.upsert_node_link(
                from_node_id=LOCAL_NODE_ID,
                to_node_id=node_id,
                status="ok",
                latency_ms=basic.latency_ms,
                last_error=None,
                probe_method="direct",
            )
            if write_capabilities:
                node = self.database.get_node(node_id)
                if node is not None:
                    # 保留旧的转发探针结果（仅 test_node 会重测转发能力）
                    self.database.update_node_capabilities(
                        node_id,
                        rsync_version=basic.rsync_version,
                        has_sshpass=basic.has_sshpass,
                        tcp_forward_ok=node["tcp_forward_ok"],  # type: ignore[arg-type]
                        agent_forward_ok=node["agent_forward_ok"],  # type: ignore[arg-type]
                    )
                    await self.events.publish(
                        "node_updated", {"node_id": node_id, "action": "capabilities"}
                    )
        else:
            link = self.database.upsert_node_link(
                from_node_id=LOCAL_NODE_ID,
                to_node_id=node_id,
                status="failed",
                latency_ms=None,
                last_error=basic.detail,
                probe_method="direct",
            )
        await self._publish_link(link)
        return link, basic

    async def _probe_hop(self, from_node_id: str, to_node_id: str) -> dict[str, object]:
        """A→B hop 探测：主控两层 SSH，B 的密钥经临时 ssh-agent + agent 转发提供。"""
        from_auth = self.resolve_auth(from_node_id)
        to_auth = self.resolve_auth(to_node_id)
        if to_auth.auth_method != "key" or not to_auth.key_path:
            raise ValueError(
                f"密码认证节点仅支持主控直连，无法探测 {from_auth.name} → {to_auth.name}"
            )

        agent = await self._start_temp_agent("probe")
        try:
            add_result = await self._run_process(
                ["ssh-add", to_auth.key_path],
                env_extra={"SSH_AUTH_SOCK": agent.sock},
                timeout_seconds=LOCAL_COMMAND_TIMEOUT_SECONDS,
            )
            if add_result.returncode != 0:
                raise ValueError(
                    f"加载节点 {to_auth.name} 的密钥到临时 agent 失败"
                    f"（密钥可能带 passphrase）: {_truncate(add_result.stderr)}"
                )
            alias = host_key_alias(to_node_id)
            # user@host 须 shlex.quote 后进入内层 sh（防御纵深，与 transfer 的
            # _build_remote_snippet 一致），安全性不单纯依赖 HOST/USERNAME 正则白名单
            target = shlex.quote(f"{to_auth.username}@{to_auth.host}")
            snippet = (
                f"echo {HOP_OK_MARKER}; "
                f"ssh -p {to_auth.port} -o BatchMode=yes -o ConnectTimeout=10 "
                f"-o NumberOfPasswordPrompts=0 -o HostKeyAlias={alias} "
                f"-o UserKnownHostsFile={REMOTE_KNOWN_HOSTS_PATH} "
                f"-o StrictHostKeyChecking=accept-new "
                f"{target} true "
                f'&& echo {LINK_OK_MARKER} || echo "EXP_SCHED_LINK_FAIL rc=$?"'
            )
            remote_command = "sh -c " + shlex.quote(snippet)
            argv, env_extra = build_ssh_command(
                from_auth,
                known_hosts_path=self.known_hosts_path(),
                remote_command=remote_command,
                forward_agent=True,
                ssh_binary=self.ssh_binary,
                sshpass_binary=self.sshpass_binary,
            )
            env_extra["SSH_AUTH_SOCK"] = agent.sock
            result = await self._run_process(
                argv, env_extra=env_extra, timeout_seconds=HOP_PROBE_TIMEOUT_SECONDS
            )
        finally:
            self._stop_temp_agent(agent)

        stdout_lines = {line.strip() for line in result.stdout.splitlines()}
        if HOP_OK_MARKER not in stdout_lines:
            # 主控→发起端这一跳失败：顺手回写 local→from，from→to 维持 unknown
            hop_detail = (
                "连接超时" if result.timed_out else translate_ssh_error(result.stderr)
            )
            hop_link = self.database.upsert_node_link(
                from_node_id=LOCAL_NODE_ID,
                to_node_id=from_node_id,
                status="failed",
                latency_ms=None,
                last_error=hop_detail,
                probe_method="direct",
            )
            await self._publish_link(hop_link)
            link = self.database.upsert_node_link(
                from_node_id=from_node_id,
                to_node_id=to_node_id,
                status="unknown",
                latency_ms=None,
                last_error=f"经主控无法连接发起端 {from_auth.name}（{hop_detail}）",
                probe_method="hop",
            )
        elif LINK_OK_MARKER not in stdout_lines:
            match = LINK_FAIL_PATTERN.search(result.stdout)
            rc_part = f"目标 SSH 退出码 {match.group(1)}：" if match else ""
            link = self.database.upsert_node_link(
                from_node_id=from_node_id,
                to_node_id=to_node_id,
                status="failed",
                latency_ms=None,
                last_error=rc_part + translate_ssh_error(result.stderr),
                probe_method="hop",
            )
        else:
            link = self.database.upsert_node_link(
                from_node_id=from_node_id,
                to_node_id=to_node_id,
                status="ok",
                # 含主控→发起端一跳，仅指示性
                latency_ms=result.duration_ms,
                last_error=None,
                probe_method="hop",
            )
        await self._publish_link(link)
        return link

    async def _probe_tcp_forward(self, auth: ResolvedAuth) -> bool | None:
        """端口转发探针：-R 0 动态分配监听端口，仅验证 sshd 是否允许 remote forward。"""
        argv, env_extra = build_ssh_command(
            auth,
            known_hosts_path=self.known_hosts_path(),
            remote_command="true",
            extra_options=["-R", "0:127.0.0.1:9", "-o", "ExitOnForwardFailure=yes"],
            ssh_binary=self.ssh_binary,
            sshpass_binary=self.sshpass_binary,
        )
        result = await self._run_process(
            argv, env_extra=env_extra, timeout_seconds=FORWARD_PROBE_TIMEOUT_SECONDS
        )
        if result.timed_out:
            return None
        return result.returncode == 0

    async def _probe_agent_forward(self, auth: ResolvedAuth) -> bool | None:
        """agent 转发探针：远端 ssh-add 退出码 2 ⇒ 转发被禁；0/1 ⇒ 转发可用。

        本地起临时空 agent 并以 ssh-add -l 验证可用（空 agent 返回 1），无法验证时返回 None。
        """
        try:
            agent = await self._start_temp_agent("test")
        except ValueError:
            return None
        try:
            check = await self._run_process(
                ["ssh-add", "-l"],
                env_extra={"SSH_AUTH_SOCK": agent.sock},
                timeout_seconds=5.0,
            )
            if check.returncode not in (0, 1):
                return None
            remote_command = "sh -c " + shlex.quote(AGENT_PROBE_SCRIPT)
            argv, env_extra = build_ssh_command(
                auth,
                known_hosts_path=self.known_hosts_path(),
                remote_command=remote_command,
                forward_agent=True,
                ssh_binary=self.ssh_binary,
                sshpass_binary=self.sshpass_binary,
            )
            env_extra["SSH_AUTH_SOCK"] = agent.sock
            result = await self._run_process(
                argv, env_extra=env_extra, timeout_seconds=AGENT_PROBE_TIMEOUT_SECONDS
            )
            match = AGENT_RC_PATTERN.search(result.stdout)
            if match is None:
                return None
            return int(match.group(1)) != 2
        finally:
            self._stop_temp_agent(agent)

    # ---------- 内部：批量探测 ----------

    @property
    def _probing(self) -> bool:
        return any(not task.done() for task in self._probe_tasks.values())

    def _inflight_edges(self) -> set[tuple[str, str]]:
        """所有未完成 sweep 任务正在探测的边集合（并集）。"""
        merged: set[tuple[str, str]] = set()
        for probe_id, task in self._probe_tasks.items():
            if not task.done():
                merged |= self._probe_edges.get(probe_id, set())
        return merged

    def _collect_probe_edges(
        self,
        pairs: Sequence[Sequence[str]] | None,
    ) -> tuple[list[tuple[str, str]], list[dict[str, object]]]:
        nodes = {str(node["id"]): node for node in self.database.list_nodes()}
        edges: list[tuple[str, str]] = []
        skipped: list[dict[str, object]] = []

        if pairs is None:
            # 全矩阵：local→X 全部 + A→B 全部有序对（B 须密钥认证）
            for to_id in nodes:
                edges.append((LOCAL_NODE_ID, to_id))
            for from_id in nodes:
                for to_id, to_node in nodes.items():
                    if from_id == to_id:
                        continue
                    if to_node["auth_method"] != "key":
                        skipped.append(
                            {"pair": [from_id, to_id], "reason": "密码认证节点仅支持主控直连"}
                        )
                        continue
                    edges.append((from_id, to_id))
            return edges, skipped

        seen: set[tuple[str, str]] = set()
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                skipped.append({"pair": list(map(str, pair)) if isinstance(pair, (list, tuple)) else [str(pair)], "reason": "无效的节点对"})
                continue
            from_id, to_id = str(pair[0]), str(pair[1])
            key = (from_id, to_id)
            if key in seen:
                continue
            seen.add(key)
            if to_id == LOCAL_NODE_ID:
                skipped.append({"pair": [from_id, to_id], "reason": "不支持指向本机的连通边"})
                continue
            if from_id == to_id:
                skipped.append({"pair": [from_id, to_id], "reason": "起点与终点相同"})
                continue
            if to_id not in nodes:
                skipped.append({"pair": [from_id, to_id], "reason": "目标节点不存在"})
                continue
            if from_id != LOCAL_NODE_ID and from_id not in nodes:
                skipped.append({"pair": [from_id, to_id], "reason": "发起端节点不存在"})
                continue
            if from_id != LOCAL_NODE_ID and nodes[to_id]["auth_method"] != "key":
                skipped.append({"pair": [from_id, to_id], "reason": "密码认证节点仅支持主控直连"})
                continue
            edges.append(key)
        return edges, skipped

    async def _run_probe_sweep(self, probe_id: str, edges: list[tuple[str, str]]) -> None:
        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def probe_one(from_id: str, to_id: str) -> None:
            async with semaphore:
                try:
                    await self.probe_edge(from_id, to_id, record_operation=False)
                except Exception as exc:
                    LOGGER.warning("连通性探测 %s→%s 失败: %s", from_id, to_id, exc)

        try:
            await asyncio.gather(*(probe_one(from_id, to_id) for from_id, to_id in edges))
        finally:
            # 完成即从在途登记中自清，保证后续防抖判断与 _probing 状态正确
            self._probe_tasks.pop(probe_id, None)
            self._probe_edges.pop(probe_id, None)
            with contextlib.suppress(Exception):
                await self.events.publish(
                    "node_links_probe_finished", {"probe_id": probe_id}
                )

    # ---------- 内部：临时 ssh-agent ----------

    def _ensure_run_dir(self) -> Path:
        run_dir = self.state_dir / "run"
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(run_dir, 0o700)
        return run_dir

    def _fallback_sock_dir(self) -> Path:
        """state_dir 过深时的 /tmp 回落目录（归属与权限严格校验，防公共目录抢注）。"""
        fallback = Path(tempfile.gettempdir()) / f"exp-sched-{os.getuid()}"
        try:
            fallback.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            pass
        else:
            # 新建目录可能受 umask/文件系统影响：收紧权限，chmod 失败不再静默吞掉
            os.chmod(fallback, 0o700)
        info = os.stat(fallback)
        if info.st_uid != os.getuid() or (info.st_mode & 0o777) != 0o700:
            raise ValueError(
                f"临时 ssh-agent 回落目录不可信: {fallback}"
                "（属主必须为当前用户且权限为 0700，可能已被其他用户抢注）"
            )
        return fallback

    async def _start_temp_agent(self, label: str) -> _TempAgent:
        run_dir = self._ensure_run_dir()
        sock = run_dir / f"{label}-{uuid4().hex[:8]}.sock"
        if len(str(sock).encode()) > _MAX_SOCKET_PATH_BYTES:
            # Unix socket 路径上限 108 字节：state_dir 过深时回落 /tmp
            sock = self._fallback_sock_dir() / f"{label}-{uuid4().hex[:8]}.sock"
        # -s 强制 sh 风格输出：SHELL 为 csh 系时默认输出 setenv 格式，
        # 否则 PID 解析必然失败，已 daemon 化的 agent 将无人回收
        result = await self._run_process(
            ["ssh-agent", "-s", "-a", str(sock)],
            timeout_seconds=LOCAL_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            # 启动失败也可能遗留 sock 占位文件，顺手回收
            with contextlib.suppress(OSError):
                os.unlink(sock)
            raise ValueError(f"无法启动临时 ssh-agent: {_truncate(result.stderr)}")
        match = SSH_AGENT_PID_PATTERN.search(result.stdout)
        if match is None:
            # agent 已 daemon 化但 PID 不可知（无法 kill）：至少回收 sock，不留占位文件
            with contextlib.suppress(OSError):
                os.unlink(sock)
            raise ValueError("无法解析临时 ssh-agent 的进程号")
        return _TempAgent(pid=int(match.group(1)), sock=str(sock))

    def _stop_temp_agent(self, agent: _TempAgent) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(agent.pid, signal.SIGTERM)
        with contextlib.suppress(OSError):
            os.unlink(agent.sock)

    # ---------- 内部：子进程与审计 ----------

    async def _run_process(
        self,
        argv: list[str],
        *,
        env_extra: dict[str, str] | None = None,
        timeout_seconds: float,
    ) -> _ProcessResult:
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError) as exc:
            return _ProcessResult(
                returncode=127,
                stdout="",
                stderr=f"无法执行命令 {argv[0]}: {exc}",
                duration_ms=0.0,
                timed_out=False,
            )
        timed_out = False
        stdout_bytes = b""
        stderr_bytes = b""
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()
        duration_ms = (time.monotonic() - started) * 1000.0
        return _ProcessResult(
            returncode=process.returncode,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            timed_out=timed_out,
        )

    async def _publish_link(self, link: dict[str, object]) -> None:
        await self.events.publish(
            "node_link_updated",
            {
                "from_node_id": link["from_node_id"],
                "to_node_id": link["to_node_id"],
                "link": link,
            },
        )

    async def _record_operation(
        self,
        *,
        action: str,
        title: str,
        level: str = "info",
        entity_type: str | None = None,
        detail: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        try:
            log = self.database.add_operation_log(
                level=level,
                source="nodes",
                action=action,
                entity_type=entity_type,
                entity_id=None,
                title=title,
                detail=detail,
                metadata=metadata,
            )
        except Exception:
            LOGGER.warning("写入操作日志失败: %s", action, exc_info=True)
            return
        await self.events.publish(
            "operation_log_created",
            {"log_id": log["id"], "action": action},
        )
