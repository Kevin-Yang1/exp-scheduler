"""rsync 文件同步服务：路由解析、命令构造、传输任务生命周期管理。

核心能力：
- resolve_route 纯函数：基于 node_links 连通性矩阵 + 节点认证方式，
  按优先级 local → direct_from_src → direct_from_dst → bridged_push → bridged_pull 选路
- 命令构造纯函数族：本机直跑 / 主控直连远端 / 远端发起（agent 转发）/ 主控 ssh -R 桥接
- classify_transfer_failure 纯函数：相位 + 退出码 + stderr 尾巴 → 错误码与中文提示
- TransferService：派发队列、进度解析（progress2）、相位机（哨兵）、取消梯、
  端口重试、运行时路由降级、服务重启恢复

安全约定：
- 子进程一律 argv 列表 + start_new_session=True，不经 shell
- 密码只经环境变量 SSHPASS 传给 sshpass，绝不进 argv / 日志 / API 响应
- 远端命令三层引用：argv 列表 → sh -c shlex.quote(snippet) → snippet 内逐项 quote
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shlex
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from .database import LOCAL_NODE_ID, Database, utc_now_iso
from .events import EventBroker
from .nodes import (
    REMOTE_KNOWN_HOSTS_PATH,
    ResolvedAuth,
    build_ssh_command,
    build_ssh_option_args,
    host_key_alias,
)

if TYPE_CHECKING:
    from .config import SchedulerConfig
    from .nodes import NodeRegistryService

LOGGER = logging.getLogger("exp_scheduler")

# ---------- 路由与桥接常量 ----------
ROUTES = ("local", "direct_from_src", "direct_from_dst", "bridged_push", "bridged_pull")
PARTIAL_DIR = ".rsync-partial"
BRIDGE_SENTINEL = "EXPSCHED_BRIDGE_READY"
EXIT_NO_MKTEMP = 96
EXIT_NO_AGENT = 97
# 避开 Linux 默认 ephemeral 端口段（32768-60999），降低与 via 上出站连接的碰撞概率
BRIDGE_PORT_RANGE = (20000, 32000)
MAX_PORT_ATTEMPTS = 3
CONNECTING_TIMEOUT_SECONDS = 45.0
FAST_FAIL_SECONDS = 15.0
STDERR_TAIL_BYTES = 8 * 1024
DEFAULT_LOG_TAIL_BYTES = 64 * 1024
DEFAULT_MAX_CONCURRENT_TRANSFERS = 2
PROGRESS_LOG_INTERVAL_SECONDS = 5.0
LOCAL_COMMAND_TIMEOUT_SECONDS = 10.0

# progress2 进度行（不加 -h，bytes 为带千分位的原始字节数；ir-chk 阶段 percent 可能回跳，照存）
PROGRESS2_RE = re.compile(
    r"^\s*(?P<bytes>[\d,]+)\s+(?P<percent>\d{1,3})%\s+"
    r"(?P<rate>[\d.]+\s*[kMGT]?B/s)\s+(?P<eta>[\d:]+)"
    r"(?:\s+\(xfr#(?P<xfr>\d+),\s+(?:ir|to)-chk=(?P<chk>\d+/\d+)\))?"
)

# rsync 额外参数白名单：形态正则 + 选项名清单（拒绝 -e/--rsh/--rsync-path 等改变执行语义的选项）
_RSYNC_ARG_PATTERN = re.compile(r"^(-z|--[a-z][a-z0-9-]*(=[^\s;|&`$<>]*)?)$")
_RSYNC_ARG_WHITELIST = {
    "-z",
    "--compress",
    "--exclude",
    "--include",
    "--bwlimit",
    "--checksum",
    "--update",
    "--ignore-existing",
    "--no-perms",
    "--chmod",
    "--timeout",
    "--whole-file",
    "--no-whole-file",
}
# 这些选项必须带 =value（裸写会让 rsync 把下一个 argv 当作其参数，吞掉路径）
_RSYNC_ARGS_REQUIRE_VALUE = {"--exclude", "--include", "--bwlimit", "--chmod", "--timeout"}

# 错误分类：可触发运行时路由降级的"连接类"错误码
_EDGE_FAILURE_CODES = {
    "via_auth_failed",
    "via_unreachable",
    "via_hostkey_changed",
    "peer_tcp_failed",
    "peer_auth_failed",
    "peer_hostkey_mismatch",
}
# 能力类失败（sshd 策略禁转发等）：可降级路由，但不回写 node_links（连通性本身没问题）
_CONNECTION_ERROR_CODES = _EDGE_FAILURE_CODES | {
    "bridge_forward_denied",
    "via_agent_forward_disabled",
    "via_no_mktemp",
    "bridge_timeout",
}

_FORWARD_FAILED_RE = re.compile(r"remote port forwarding failed for listen (port|path)")
_RSYNC_ERROR_CODE_RE = re.compile(r"rsync error: .*\(code (\d+)\)")
_SSH_AGENT_PID_RE = re.compile(r"SSH_AGENT_PID=(\d+)")
_MAX_SOCKET_PATH_BYTES = 100
# 并发上限最大可调值（get_settings/update_settings 的 8 上限）
_MAX_TRANSFER_CONCURRENCY_CAP = 8
# 专属线程池容量：最大并发 × 每任务 3 个阻塞调用（process.wait + stdout/stderr reader）+ 4 冗余
_EXECUTOR_MAX_WORKERS = _MAX_TRANSFER_CONCURRENCY_CAP * 3 + 4
# agent socket 路径超长时的回落根目录（测试可替换）
_FALLBACK_TMP_ROOT = Path("/tmp")


# ---------- 纯函数：参数校验 ----------


def validate_rsync_args(args: Sequence[str]) -> list[str]:
    """白名单校验 rsync 额外参数，非法参数抛中文 ValueError。"""
    validated: list[str] = []
    for raw in args:
        arg = str(raw).strip()
        if not arg:
            continue
        if not _RSYNC_ARG_PATTERN.match(arg):
            raise ValueError(f"不允许的 rsync 参数格式: {arg}")
        name = arg.split("=", 1)[0]
        if name not in _RSYNC_ARG_WHITELIST:
            raise ValueError(f"rsync 参数不在白名单内: {arg}")
        if name in _RSYNC_ARGS_REQUIRE_VALUE and "=" not in arg:
            raise ValueError(f"rsync 参数 {name} 必须使用 {name}=值 的形式")
        validated.append(arg)
    return validated


# ---------- 纯函数：路由解析 ----------


def resolve_route(
    src: str,
    dst: str,
    links: Mapping[tuple[str, str], str],
    auth_methods: Mapping[str, str],
) -> dict[str, object]:
    """基于连通性矩阵快照解析候选路由。

    links: (from, to) -> 'unknown'|'ok'|'failed'，缺键 ≡ unknown
    auth_methods: node_id -> 'key'|'password'（'local' 不在其中）
    """
    candidates: list[dict[str, object]] = []

    def add(route: str, edges: list[tuple[str, str]], hard_reasons: list[str]) -> None:
        reasons = list(hard_reasons)
        probes: list[list[str]] = []
        hard = bool(hard_reasons)
        for from_id, to_id in edges:
            status = str(links.get((from_id, to_id), "unknown"))
            if status == "failed":
                reasons.append(f"链路 {from_id} → {to_id} 此前探测失败")
                hard = True
            elif status != "ok":
                probes.append([from_id, to_id])
        candidates.append(
            {
                "route": route,
                "feasible": not hard,
                "reasons": reasons,
                "requires_probe": probes if not hard else [],
            }
        )

    if src == LOCAL_NODE_ID and dst == LOCAL_NODE_ID:
        candidates.append(
            {"route": "local", "feasible": True, "reasons": [], "requires_probe": []}
        )
    else:
        # direct_from_src：rsync 跑在 src 上（src='local' 时即主控直连 push）
        if src == LOCAL_NODE_ID:
            add("direct_from_src", [(LOCAL_NODE_ID, dst)], [])
        elif dst == LOCAL_NODE_ID:
            candidates.append(
                {
                    "route": "direct_from_src",
                    "feasible": False,
                    "reasons": ["本机不能作为远端连入目标，请使用从目标端发起的直连（direct_from_dst）"],
                    "requires_probe": [],
                }
            )
        else:
            hard: list[str] = []
            if auth_methods.get(dst) != "key":
                hard.append("目标节点为密码认证，无法由远端发起端连入（密码会暴露在发起端进程列表）")
            add("direct_from_src", [(LOCAL_NODE_ID, src), (src, dst)], hard)

        # direct_from_dst：rsync 跑在 dst 上（dst='local' 时即主控直连 pull）
        if dst == LOCAL_NODE_ID:
            add("direct_from_dst", [(LOCAL_NODE_ID, src)], [])
        elif src == LOCAL_NODE_ID:
            candidates.append(
                {
                    "route": "direct_from_dst",
                    "feasible": False,
                    "reasons": ["本机不能作为远端连入目标，请使用从源端发起的直连（direct_from_src）"],
                    "requires_probe": [],
                }
            )
        else:
            hard = []
            if auth_methods.get(src) != "key":
                hard.append("源节点为密码认证，无法由远端发起端连入（密码会暴露在发起端进程列表）")
            add("direct_from_dst", [(LOCAL_NODE_ID, dst), (dst, src)], hard)

        # 桥接：仅 src、dst 均为远端时适用
        if src != LOCAL_NODE_ID and dst != LOCAL_NODE_ID:
            push_hard = (
                [] if auth_methods.get(dst) == "key" else ["桥接推送要求目标节点为密钥认证"]
            )
            add("bridged_push", [(LOCAL_NODE_ID, src), (LOCAL_NODE_ID, dst)], push_hard)
            pull_hard = (
                [] if auth_methods.get(src) == "key" else ["桥接拉取要求源节点为密钥认证"]
            )
            add("bridged_pull", [(LOCAL_NODE_ID, src), (LOCAL_NODE_ID, dst)], pull_hard)

    recommended: str | None = None
    needs_probe = False
    for cand in candidates:
        if cand["feasible"] and not cand["requires_probe"]:
            recommended = str(cand["route"])
            break
    if recommended is None:
        for cand in candidates:
            if cand["feasible"]:
                recommended = str(cand["route"])
                needs_probe = True
                break
    return {"candidates": candidates, "recommended": recommended, "needs_probe": needs_probe}


# ---------- 纯函数：命令构造 ----------


@dataclass(slots=True)
class TransferCommand:
    """构造完成的传输命令（redacted 不含任何密码，可直接写日志）。"""

    argv: list[str]
    env_extra: dict[str, str]
    redacted: str
    listen_port: int | None = None


def _format_host(host: str) -> str:
    """IPv6 等含 ':' 的主机在 rsync spec / -R 串中需加方括号。"""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _remote_spec(username: str, host: str, path: str) -> str:
    return f"{username}@{_format_host(host)}:{path}"


def build_rsync_base_args(
    *,
    rsync_args: Sequence[str] = (),
    delete_extras: bool = False,
    dry_run: bool = False,
    rsync_binary: str = "rsync",
) -> list[str]:
    """通用 rsync 头（--partial-dir 防截断文件留在最终文件名；不加 -h 便于解析原始字节）。"""
    argv = [
        rsync_binary,
        "-a",
        f"--partial-dir={PARTIAL_DIR}",
        "--info=progress2",
        "--info=name1",
        "-s",
        "--timeout=300",
        *validate_rsync_args(rsync_args),
    ]
    if delete_extras:
        argv.append("--delete")
    if dry_run:
        argv.append("--dry-run")
    return argv


def build_local_rsync(
    src_path: str,
    dst_path: str,
    *,
    rsync_args: Sequence[str] = (),
    delete_extras: bool = False,
    dry_run: bool = False,
    rsync_binary: str = "rsync",
) -> TransferCommand:
    """本机直跑 rsync（src=dst='local'）。"""
    argv = build_rsync_base_args(
        rsync_args=rsync_args,
        delete_extras=delete_extras,
        dry_run=dry_run,
        rsync_binary=rsync_binary,
    ) + [src_path, dst_path]
    return TransferCommand(argv=argv, env_extra={}, redacted=shlex.join(argv))


def build_controller_rsync(
    *,
    direction: Literal["push", "pull"],
    remote: ResolvedAuth,
    local_path: str,
    remote_path: str,
    known_hosts_path: Path | str,
    rsync_args: Sequence[str] = (),
    delete_extras: bool = False,
    dry_run: bool = False,
    rsync_binary: str = "rsync",
    ssh_binary: str = "ssh",
    sshpass_binary: str = "sshpass",
) -> TransferCommand:
    """主控直连远端（direct_from_src 且 src='local'，或 direct_from_dst 且 dst='local'）。"""
    if remote.is_local or not remote.host or not remote.username:
        raise ValueError(f"节点 {remote.name} 缺少远端连接配置")
    transport = [ssh_binary, *build_ssh_option_args(port=remote.port, known_hosts_path=known_hosts_path)]
    prefix: list[str] = []
    env_extra: dict[str, str] = {}
    if remote.auth_method == "password":
        if not remote.password:
            raise ValueError(f"节点 {remote.name} 未配置密码")
        transport += [
            "-o",
            "PreferredAuthentications=password,keyboard-interactive",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=1",
        ]
        prefix = [sshpass_binary, "-e"]
        env_extra["SSHPASS"] = remote.password
    elif remote.auth_method == "key":
        if not remote.key_path:
            raise ValueError(f"节点 {remote.name} 未配置 SSH 密钥")
        transport += ["-i", remote.key_path, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
    else:
        raise ValueError(f"节点 {remote.name} 的认证方式无效: {remote.auth_method}")

    rsync_argv = build_rsync_base_args(
        rsync_args=rsync_args,
        delete_extras=delete_extras,
        dry_run=dry_run,
        rsync_binary=rsync_binary,
    ) + ["-e", shlex.join(transport)]
    spec = _remote_spec(remote.username, remote.host, remote_path)
    if direction == "push":
        rsync_argv += [local_path, spec]
    else:
        rsync_argv += [spec, local_path]
    argv = prefix + rsync_argv
    return TransferCommand(argv=argv, env_extra=env_extra, redacted=shlex.join(argv))


def _build_inner_e_value(
    *,
    port: int,
    alias: str,
    known_hosts_ref: str,
    strict: bool,
) -> str:
    """inner ssh 的 -e 值（snippet 内用双引号包裹，$kh/$HOME 由 via 的 sh 展开）。"""
    strictness = "yes" if strict else "accept-new"
    return (
        f"ssh -p {port} -o HostKeyAlias={alias}"
        f" -o UserKnownHostsFile={known_hosts_ref}"
        f" -o StrictHostKeyChecking={strictness}"
        " -o BatchMode=yes -o ConnectTimeout=10"
        " -o ServerAliveInterval=15 -o ServerAliveCountMax=6"
    )


def _build_remote_snippet(
    *,
    rsync_head: Sequence[str],
    inner_e: str,
    src_spec: str,
    dst_spec: str,
    hostkey_lines: Sequence[str],
) -> str:
    """via 上经 sh -c 执行的 snippet：可选 hostkey 钉死 + agent 前置检查 + 哨兵 + rsync。"""
    rsync_words = (
        " ".join(shlex.quote(word) for word in rsync_head)
        + f' -e "{inner_e}" '
        + f"{shlex.quote(src_spec)} {shlex.quote(dst_spec)}"
    )
    parts: list[str] = []
    if hostkey_lines:
        quoted_lines = " ".join(shlex.quote(line) for line in hostkey_lines)
        parts.append(f"kh=$(mktemp) || exit {EXIT_NO_MKTEMP}")
        parts.append("trap 'rm -f \"$kh\"' EXIT")
        parts.append(f"printf '%s\\n' {quoted_lines} >\"$kh\"")
    parts.append(
        f'[ -n "$SSH_AUTH_SOCK" ] || {{ echo EXPSCHED_NO_AGENT >&2; exit {EXIT_NO_AGENT}; }}'
    )
    parts.append(f"echo {BRIDGE_SENTINEL}")
    parts.append(rsync_words)
    return "; ".join(parts)


def build_remote_initiated(
    *,
    via: ResolvedAuth,
    peer: ResolvedAuth,
    direction: Literal["push", "pull"],
    src_path: str,
    dst_path: str,
    known_hosts_path: Path | str,
    agent_sock: str,
    job_id: str,
    peer_hostkey_lines: Sequence[str] | None = None,
    rsync_args: Sequence[str] = (),
    delete_extras: bool = False,
    dry_run: bool = False,
    ssh_binary: str = "ssh",
    sshpass_binary: str = "sshpass",
) -> TransferCommand:
    """远端发起的直连（direct_from_src/dst 的 remote 形态）：inner ssh 直连 peer 真实地址。

    主控 known_hosts 有 peer 记录时 mktemp 注入 + StrictHostKeyChecking=yes 钉死；
    无记录（主控不可达 peer）时回落 via 上 ~/.exp-scheduler.known_hosts + accept-new TOFU。
    """
    if peer.is_local or not peer.host or not peer.username:
        raise ValueError(f"节点 {peer.name} 缺少远端连接配置")
    hostkey_lines = list(peer_hostkey_lines or [])
    strict = bool(hostkey_lines)
    alias = host_key_alias(peer.node_id)
    inner_e = _build_inner_e_value(
        port=peer.port,
        alias=alias,
        known_hosts_ref="$kh" if strict else REMOTE_KNOWN_HOSTS_PATH,
        strict=strict,
    )
    peer_host = _format_host(peer.host)
    if direction == "push":
        src_spec, dst_spec = src_path, f"{peer.username}@{peer_host}:{dst_path}"
    else:
        src_spec, dst_spec = f"{peer.username}@{peer_host}:{src_path}", dst_path
    snippet = _build_remote_snippet(
        rsync_head=build_rsync_base_args(
            rsync_args=rsync_args, delete_extras=delete_extras, dry_run=dry_run
        ),
        inner_e=inner_e,
        src_spec=src_spec,
        dst_spec=dst_spec,
        hostkey_lines=hostkey_lines,
    )
    argv, env_extra = build_ssh_command(
        via,
        known_hosts_path=known_hosts_path,
        remote_command="sh -c " + shlex.quote(snippet),
        forward_agent=True,
        extra_options=["-T"],
        ssh_binary=ssh_binary,
        sshpass_binary=sshpass_binary,
    )
    env_extra["SSH_AUTH_SOCK"] = agent_sock
    env_extra["EXPSCHED_JOB_ID"] = job_id
    return TransferCommand(argv=argv, env_extra=env_extra, redacted=shlex.join(argv))


def build_bridged_command(
    *,
    via: ResolvedAuth,
    peer: ResolvedAuth,
    direction: Literal["push", "pull"],
    src_path: str,
    dst_path: str,
    listen_port: int,
    peer_hostkey_lines: Sequence[str],
    known_hosts_path: Path | str,
    agent_sock: str,
    job_id: str,
    rsync_args: Sequence[str] = (),
    delete_extras: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    ssh_binary: str = "ssh",
    sshpass_binary: str = "sshpass",
) -> TransferCommand:
    """主控 ssh -R 桥接：via 上 rsync 经 127.0.0.1:{P} 隧道到 peer，回连 TCP 由主控发起。"""
    if peer.is_local or not peer.host or not peer.username:
        raise ValueError(f"节点 {peer.name} 缺少远端连接配置")
    if not peer_hostkey_lines:
        raise ValueError(
            f"主控 known_hosts 中没有节点 {peer.name} 的主机密钥记录，"
            "请先在节点页对其执行一次测试连接"
        )
    alias = host_key_alias(peer.node_id)
    inner_e = _build_inner_e_value(
        port=listen_port, alias=alias, known_hosts_ref="$kh", strict=True
    )
    if direction == "push":
        src_spec, dst_spec = src_path, f"{peer.username}@127.0.0.1:{dst_path}"
    else:
        src_spec, dst_spec = f"{peer.username}@127.0.0.1:{src_path}", dst_path
    snippet = _build_remote_snippet(
        rsync_head=build_rsync_base_args(
            rsync_args=rsync_args, delete_extras=delete_extras, dry_run=dry_run
        ),
        inner_e=inner_e,
        src_spec=src_spec,
        dst_spec=dst_spec,
        hostkey_lines=list(peer_hostkey_lines),
    )
    extra_options = [
        "-T",
        "-R",
        f"127.0.0.1:{listen_port}:{_format_host(peer.host)}:{peer.port}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
    ]
    if verbose:
        # 末次重试加 -v：stderr 含 "Server has disabled port forwarding." 时可确定性判定禁转发
        extra_options.insert(0, "-v")
    argv, env_extra = build_ssh_command(
        via,
        known_hosts_path=known_hosts_path,
        remote_command="sh -c " + shlex.quote(snippet),
        forward_agent=True,
        extra_options=extra_options,
        ssh_binary=ssh_binary,
        sshpass_binary=sshpass_binary,
    )
    env_extra["SSH_AUTH_SOCK"] = agent_sock
    env_extra["EXPSCHED_JOB_ID"] = job_id
    return TransferCommand(
        argv=argv, env_extra=env_extra, redacted=shlex.join(argv), listen_port=listen_port
    )


# ---------- 纯函数：进度解析与错误分类 ----------


def parse_progress_line(line: str) -> dict[str, object] | None:
    """解析 progress2 进度行，返回 {bytes, percent, rate, eta, xfr} 或 None。"""
    match = PROGRESS2_RE.match(line)
    if match is None:
        return None
    return {
        "bytes": int(match.group("bytes").replace(",", "")),
        "percent": int(match.group("percent")),
        "rate": match.group("rate"),
        "eta": match.group("eta"),
        "xfr": int(match.group("xfr")) if match.group("xfr") else None,
    }


def classify_transfer_failure(
    phase: str,
    exit_code: int | None,
    stderr_tail: str,
    listen_port: int | None = None,
) -> tuple[str, str, bool]:
    """失败分类：(error_code, 中文提示, retry_with_new_port)。

    判定顺序：专属退出码（96/97）→ 端口转发失败 → 相位 + stderr 正则自上而下首匹配 → 兜底。
    桥接下（listen_port 非 None）inner 连接类失败（refused/reset/kex/host key mismatch
    且行含 127.0.0.1 或 alias）也返回 retry_with_new_port=True——sshd 部分绑定误路由防御。
    """
    text = stderr_tail or ""
    lowered = text.lower()
    bridged = listen_port is not None

    if exit_code == EXIT_NO_MKTEMP:
        return ("via_no_mktemp", "发起端缺少 mktemp 命令，无法准备临时 known_hosts 文件。", False)
    if exit_code == EXIT_NO_AGENT or "EXPSCHED_NO_AGENT" in text:
        return (
            "via_agent_forward_disabled",
            "发起端 sshd 禁用了 agent 转发（AllowAgentForwarding no），"
            "无法向对端做密钥认证。可放开该配置，或调换传输方向。",
            False,
        )
    if _FORWARD_FAILED_RE.search(text):
        return (
            "bridge_forward_denied",
            f"发起端 sshd 拒绝建立端口转发（监听端口 {listen_port}）：可能端口被占用，"
            "也可能是 AllowTcpForwarding no。",
            bridged,
        )

    if phase == "connecting":
        if "permission denied" in lowered or "too many authentication failures" in lowered:
            return (
                "via_auth_failed",
                "无法登录发起端：认证失败。请到节点页重新测试连接，检查密钥/密码配置。",
                False,
            )
        if (
            "ssh: connect to host" in lowered
            or "could not resolve hostname" in lowered
            or "no route to host" in lowered
            or "connection timed out" in lowered
        ):
            return ("via_unreachable", "无法连接发起端：网络不可达或 sshd 未运行。", False)
        if "identification has changed" in lowered or "host key verification failed" in lowered:
            return (
                "via_hostkey_changed",
                "发起端主机密钥发生变化（可能重装系统，也可能存在中间人）。"
                "确认无误后在节点页重新测试连接更新记录。",
                False,
            )

    # transferring 相位（或 connecting 未匹配 via 类错误时的兜底）
    inner_marker = "127.0.0.1" in text or "expsched-" in lowered
    if "host key verification failed" in lowered or "identification has changed" in lowered:
        if inner_marker:
            return (
                "peer_hostkey_mismatch",
                "对端主机密钥与本机记录不符（可能重装系统或存在中间人）。"
                "请在节点页对对端重新测试连接更新记录后重试。",
                bridged,
            )
        return (
            "via_hostkey_changed",
            "主机密钥发生变化（可能重装系统，也可能存在中间人）。请在节点页重新测试连接。",
            False,
        )
    if "permission denied" in lowered or "too many authentication failures" in lowered:
        return (
            "peer_auth_failed",
            "对端密钥认证失败：请确认配置的密钥已加入对端的 ~/.ssh/authorized_keys。",
            False,
        )
    if (
        re.search(r"connect to host .+? port \d+", lowered)
        or "connection refused" in lowered
        or "connection reset" in lowered
        or "connection closed by" in lowered
        or "kex_exchange_identification" in lowered
        or "broken pipe" in lowered
    ):
        return (
            "peer_tcp_failed",
            "无法建立到对端的连接（桥接回连由主控执行）。请检查主控到对端的网络与 sshd。",
            bridged and inner_marker,
        )
    if "no space left" in lowered:
        return ("dst_disk_full", "目的端磁盘空间不足。", False)

    code_match = _RSYNC_ERROR_CODE_RE.search(text)
    rsync_code = int(code_match.group(1)) if code_match else None
    if rsync_code is None and exit_code in (23, 24):
        rsync_code = exit_code
    if rsync_code in (23, 24):
        return (
            "rsync_partial",
            "部分文件未能传输（权限不足或源文件在传输中被修改/删除），详情见日志。",
            False,
        )
    if rsync_code is not None:
        return ("rsync_failed", f"rsync 退出码 {rsync_code}：传输失败，详情见日志。", False)
    if exit_code is not None and exit_code < 0:
        return ("killed", f"传输进程被信号终止（信号 {-exit_code}）。", False)
    if phase == "connecting":
        return ("via_unreachable", "建立连接阶段失败，详情见日志。", False)
    return ("rsync_failed", f"传输失败（退出码 {exit_code}），详情见日志。", False)


def _blamed_edge(
    route: str, src: str, dst: str, error_code: str
) -> tuple[str, str] | None:
    """运行时降级回写 node_links 时，由路由 + 错误码定位应标记 failed 的边。"""
    if error_code not in _EDGE_FAILURE_CODES:
        return None
    via_side = error_code.startswith("via_")
    if route == "direct_from_src":
        if src == LOCAL_NODE_ID:
            return (LOCAL_NODE_ID, dst)
        return (LOCAL_NODE_ID, src) if via_side else (src, dst)
    if route == "direct_from_dst":
        if dst == LOCAL_NODE_ID:
            return (LOCAL_NODE_ID, src)
        return (LOCAL_NODE_ID, dst) if via_side else (dst, src)
    if route == "bridged_push":
        # peer 类失败 = 主控→dst 的回连失败
        return (LOCAL_NODE_ID, src) if via_side else (LOCAL_NODE_ID, dst)
    if route == "bridged_pull":
        return (LOCAL_NODE_ID, dst) if via_side else (LOCAL_NODE_ID, src)
    return None


# ---------- 服务内部数据结构 ----------


@dataclass(slots=True)
class _JobAgent:
    pid: int
    sock: str


@dataclass(slots=True)
class _JobHandle:
    job_id: str
    process: subprocess.Popen[bytes] | None = None
    agent: _JobAgent | None = None
    cancel_requested: bool = False
    # 在途取消梯任务（SIGINT→SIGTERM→SIGKILL）；用于判断是否需要补发信号
    cancel_ladder: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _StreamState:
    job_id: str
    phase: str
    timed_out: bool = False
    bytes_transferred: int | None = None
    percent: float | None = None
    files_transferred: int | None = None
    last_update_percent: float = -10.0
    last_update_time: float = 0.0
    last_progress_log_time: float = 0.0
    stderr_buf: bytearray = field(default_factory=bytearray)

    def stderr_tail(self) -> str:
        return bytes(self.stderr_buf).decode("utf-8", errors="replace")


@dataclass(slots=True)
class _SpawnResult:
    exit_code: int | None
    phase: str
    stderr_tail: str
    timed_out: bool
    bytes_transferred: int | None
    duration_seconds: float


@dataclass(slots=True)
class _AttemptResult:
    status: str  # 'succeeded' | 'failed' | 'cancelled'
    exit_code: int | None = None
    error_code: str | None = None
    error: str | None = None
    duration_seconds: float = 0.0
    bytes_transferred: int | None = None
    listen_port: int | None = None

# ---------- TransferService ----------


class TransferService:
    """rsync 传输任务服务：排队派发、进度推送、取消梯、运行时路由降级、重启恢复。"""

    def __init__(
        self,
        *,
        config: SchedulerConfig,
        database: Database,
        events: EventBroker,
        nodes: NodeRegistryService,
        rsync_binary: str = "rsync",
        ssh_binary: str = "ssh",
        sshpass_binary: str = "sshpass",
    ) -> None:
        self.config = config
        self.database = database
        self.events = events
        self.nodes = nodes
        self.rsync_binary = rsync_binary
        self.ssh_binary = ssh_binary
        self.sshpass_binary = sshpass_binary
        # ssh-agent / ssh-add 走 PATH，可由测试替换
        self.ssh_agent_binary = "ssh-agent"
        self.ssh_add_binary = "ssh-add"
        self.state_dir = Path(config.state_dir)
        self.log_dir = Path(config.log_dir)
        self._dispatch_lock = asyncio.Lock()
        self._handles: dict[str, _JobHandle] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        # 进程内在途桥接端口（dict[via_node_id, set[port]]），防并发任务在同一 via 自撞
        self._ports_in_use: dict[str, set[int]] = {}
        self._shutting_down = False
        # 专属线程池：长阻塞的 reader/process.wait 不与全局默认池（调度器 GPU 轮询等）抢线程
        self._executor = ThreadPoolExecutor(
            max_workers=_EXECUTOR_MAX_WORKERS, thread_name_prefix="transfer-io"
        )

    # ---------- 生命周期 ----------

    async def startup(self) -> None:
        """重启恢复：running → interrupted，验证 pid 归属后清理遗留进程与 agent。"""
        self._shutting_down = False
        interrupted = self.database.mark_running_transfer_jobs_interrupted()
        for job in interrupted:
            job_id = str(job["id"])
            pid = job.get("pid")
            # 先验 /proc/{pid}/environ 含 EXPSCHED_JOB_ID 再 killpg，防 pid 复用误杀
            if pid and _proc_env_has(int(pid), f"EXPSCHED_JOB_ID={job_id}"):  # type: ignore[arg-type]
                self._signal_group(int(pid), signal.SIGTERM)  # type: ignore[arg-type]
            agent_pid = job.get("agent_pid")
            # agent 归属校验：cmdline 须含该 job 的专属 sock 路径（agent-{job_id}.sock），
            # 不匹配即 pid 已被复用给无关进程——只清记录，不杀进程
            if agent_pid and _proc_cmdline_contains(int(agent_pid), f"agent-{job_id}.sock"):  # type: ignore[arg-type]
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(int(agent_pid), signal.SIGTERM)  # type: ignore[arg-type]
            with contextlib.suppress(ValueError):
                self.database.update_transfer_job(job_id, pid=None, agent_pid=None)
            await self.events.publish(
                "transfer_finished",
                {"job_id": job_id, "status": "interrupted", "job": self.database.get_transfer_job(job_id)},
            )
        if interrupted:
            await self._record_operation(
                level="warning",
                action="transfer_interrupted",
                title=f"服务重启，{len(interrupted)} 个传输任务被标记为中断",
                metadata={"job_ids": [str(job["id"]) for job in interrupted]},
            )
        # 清扫孤儿 agent：state_dir/run 与 /tmp 回落目录都扫，
        # 先按 /proc cmdline 含 sock 路径回收残留进程，再删除 socket 文件
        for agent_dir in (self.state_dir / "run", _fallback_agent_dir()):
            if not agent_dir.is_dir():
                continue
            for sock in agent_dir.glob("agent-*.sock"):
                for orphan_pid in _find_pids_by_cmdline_arg(str(sock)):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(orphan_pid, signal.SIGTERM)
                with contextlib.suppress(OSError):
                    sock.unlink()
        await self._dispatch()

    async def shutdown(self) -> None:
        """优雅停机：对 running 进程组 SIGTERM → 3s → SIGKILL，标 interrupted。"""
        self._shutting_down = True
        for handle in list(self._handles.values()):
            handle.cancel_requested = True
            process = handle.process
            if process is not None and process.poll() is None:
                self._signal_group(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not any(
                h.process is not None and h.process.poll() is None
                for h in self._handles.values()
            ):
                break
            await asyncio.sleep(0.1)
        for handle in list(self._handles.values()):
            process = handle.process
            if process is not None and process.poll() is None:
                self._signal_group(process.pid, signal.SIGKILL)
        watchers = [task for task in self._watchers.values() if not task.done()]
        if watchers:
            done, pending = await asyncio.wait(watchers, timeout=5.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        # watcher 因 cancel_requested 多半已标 cancelled；兜底把残余 running 标 interrupted
        self.database.mark_running_transfer_jobs_interrupted(error="服务停止，传输中断")
        self._executor.shutdown(wait=False)

    # ---------- 创建 / 查询 ----------

    async def plan_routes(
        self,
        src_node_id: str,
        src_path: str,
        dst_node_id: str,
        dst_path: str,
        probe: bool = False,
    ) -> dict[str, object]:
        """路由预览：resolve_route 包装；probe=True 时就地探测推荐项的 unknown 边后重解析。"""
        self.nodes.resolve_auth(src_node_id)
        self.nodes.resolve_auth(dst_node_id)
        del src_path, dst_path  # 路径暂不参与路由计算，保留以与创建接口一致
        # 与 create_job 契约一致：同一远程节点不支持传输，预览直接返回空候选 + 明确原因
        if src_node_id == dst_node_id and src_node_id != LOCAL_NODE_ID:
            return {
                "candidates": [],
                "recommended": None,
                "needs_probe": False,
                "reason": "源与目标为同一远程节点时暂不支持传输（可在该节点终端直接复制）",
            }
        links, auths = self._matrix_snapshot()
        resolution = resolve_route(src_node_id, dst_node_id, links, auths)
        if probe and resolution["needs_probe"] and resolution["recommended"]:
            cand = next(
                c
                for c in resolution["candidates"]  # type: ignore[union-attr]
                if c["route"] == resolution["recommended"]
            )
            for edge in list(cand["requires_probe"]):  # type: ignore[union-attr]
                with contextlib.suppress(ValueError):
                    await self.nodes.probe_edge(str(edge[0]), str(edge[1]))
            links, auths = self._matrix_snapshot()
            resolution = resolve_route(src_node_id, dst_node_id, links, auths)
        return resolution

    async def create_job(
        self,
        src_node_id: str,
        src_path: str,
        dst_node_id: str,
        dst_path: str,
        *,
        name: str | None = None,
        rsync_args: Sequence[str] | None = None,
        delete_extras: bool = False,
        dry_run: bool = False,
        route: str = "auto",
        probe_unknown: bool = True,
        src_contents_only: bool = False,
    ) -> dict[str, object]:
        validated_args = validate_rsync_args(list(rsync_args or []))
        src_auth = self.nodes.resolve_auth(src_node_id)
        dst_auth = self.nodes.resolve_auth(dst_node_id)
        src_path = self._normalize_path(src_path, label="源路径")
        dst_path = self._normalize_path(dst_path, label="目标路径")
        # trailing slash 语义：仅复制目录内容 → 尾 '/'；复制目录本身 → 去尾 '/'
        if src_contents_only:
            src_path = (src_path.rstrip("/") or "") + "/"
        elif src_path != "/":
            src_path = src_path.rstrip("/") or "/"
        if dst_path != "/":
            dst_path = dst_path.rstrip("/") or "/"
        if src_node_id == dst_node_id and src_node_id != LOCAL_NODE_ID:
            raise ValueError("源与目标为同一远程节点时暂不支持传输（可在该节点终端直接复制）")
        if route != "auto" and route not in ROUTES:
            raise ValueError(f"未知路由: {route}")

        links, auths = self._matrix_snapshot()
        resolution = resolve_route(src_node_id, dst_node_id, links, auths)
        if route == "auto":
            resolved_by = "auto"
            chosen = await self._resolve_with_probes(
                src_node_id, dst_node_id, resolution, probe_unknown=probe_unknown
            )
        else:
            resolved_by = "manual"
            cand = next(
                (c for c in resolution["candidates"] if c["route"] == route),  # type: ignore[union-attr]
                None,
            )
            if cand is None:
                raise ValueError(f"路由 {route} 不适用于该源/目标组合")
            if not cand["feasible"]:
                raise ValueError(
                    f"指定路由 {route} 不可行：" + "；".join(str(r) for r in cand["reasons"])  # type: ignore[union-attr]
                )
            chosen = route

        job_id = uuid4().hex
        snapshot = {"src": _auth_snapshot(src_auth), "dst": _auth_snapshot(dst_auth)}
        job = self.database.create_transfer_job(
            job_id=job_id,
            name=(name or "").strip() or None,
            src_node_id=src_node_id,
            src_path=src_path,
            dst_node_id=dst_node_id,
            dst_path=dst_path,
            route=chosen,
            route_resolved_by=resolved_by,
            rsync_args=validated_args,
            delete_extras=delete_extras,
            dry_run=dry_run,
            node_snapshot=snapshot,
        )
        await self._record_operation(
            level="warning" if delete_extras else "info",
            action="transfer_created",
            entity_id=job_id,
            title=f"创建传输任务: {src_node_id}:{src_path} → {dst_node_id}:{dst_path}",
            detail=f"路由 {chosen}（{resolved_by}）"
            + ("；启用 --delete（目标端多余文件将被删除）" if delete_extras else "")
            + ("；dry-run 演练" if dry_run else ""),
            metadata={"job_id": job_id, "route": chosen, "dry_run": dry_run},
        )
        await self.events.publish("transfer_created", {"job_id": job_id, "job": job})
        await self._dispatch()
        return self.database.get_transfer_job(job_id) or job

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        return self.database.list_transfer_jobs(status=status, limit=limit, offset=offset)

    async def read_job_log(
        self,
        job_id: str,
        tail_bytes: int | None = DEFAULT_LOG_TAIL_BYTES,
    ) -> dict[str, object]:
        job = self.database.get_transfer_job(job_id)
        if job is None:
            raise ValueError(f"传输任务不存在: {job_id}")
        log_path = job.get("log_path")
        path = Path(str(log_path)) if log_path else None
        if path is None or not path.is_file():
            return {"content": "", "log_path": log_path, "size": 0}
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if tail_bytes is not None and size > tail_bytes:
                fh.seek(size - tail_bytes)
            data = fh.read()
        return {
            "content": data.decode("utf-8", errors="replace"),
            "log_path": str(path),
            "size": size,
        }

    # ---------- 取消 / 删除 ----------

    async def cancel_job(self, job_id: str) -> dict[str, object]:
        job = self.database.get_transfer_job(job_id)
        if job is None:
            raise ValueError(f"传输任务不存在: {job_id}")
        status = str(job["status"])
        if status == "pending":
            # 与 _dispatch 串行化，避免取消与启动竞态
            async with self._dispatch_lock:
                fresh = self.database.get_transfer_job(job_id)
                if fresh is not None and fresh["status"] == "pending":
                    updated = self.database.update_transfer_job(
                        job_id,
                        status="cancelled",
                        finished_at=utc_now_iso(),
                        error="已取消",
                    )
                    await self._record_operation(
                        action="transfer_cancelled",
                        entity_id=job_id,
                        title="取消排队中的传输任务",
                        metadata={"job_id": job_id},
                    )
                    await self.events.publish(
                        "transfer_finished",
                        {"job_id": job_id, "status": "cancelled", "job": updated},
                    )
                    return updated
            job = self.database.get_transfer_job(job_id) or job
            status = str(job["status"])
        if status == "running":
            handle = self._handles.get(job_id)
            if handle is None:
                # 无在管进程（异常残留）：直接标记
                updated = self.database.update_transfer_job(
                    job_id, status="cancelled", finished_at=utc_now_iso(), error="已取消"
                )
                await self.events.publish(
                    "transfer_finished",
                    {"job_id": job_id, "status": "cancelled", "job": updated},
                )
                return updated
            if not handle.cancel_requested:
                handle.cancel_requested = True
                # 取消梯：SIGINT → 5s → SIGTERM → 5s → SIGKILL（对齐 scheduler 先例）；
                # 进程尚未 spawn（如 ssh-agent 准备窗口）时由 _spawn_and_watch 补走取消梯
                self._begin_cancel_ladder(handle)
                self._log_job(job_id, "收到取消请求，正在终止传输进程")
                await self._record_operation(
                    action="transfer_cancelled",
                    entity_id=job_id,
                    title="取消运行中的传输任务",
                    metadata={"job_id": job_id},
                )
            elif self._begin_cancel_ladder(handle):
                # 首次取消落在进程启动前的准备窗口：进程已起且无取消梯在途时允许补发信号
                self._log_job(job_id, "再次收到取消请求，向传输进程补发终止信号")
            return self.database.get_transfer_job(job_id) or job
        raise ValueError(f"传输任务已处于 {status} 状态，无法取消")

    async def delete_job(self, job_id: str) -> None:
        job = self.database.get_transfer_job(job_id)
        if job is None:
            raise ValueError(f"传输任务不存在: {job_id}")
        if job["status"] == "running":
            raise ValueError("传输任务运行中，无法删除，请先取消")
        self.database.delete_transfer_job(job_id)
        log_path = job.get("log_path")
        if log_path:
            with contextlib.suppress(OSError):
                Path(str(log_path)).unlink()
        await self._record_operation(
            action="transfer_deleted",
            entity_id=job_id,
            title="删除传输任务",
            metadata={"job_id": job_id, "status": job["status"]},
        )
        await self.events.publish("transfer_deleted", {"job_id": job_id})

    # ---------- 设置 ----------

    async def get_settings(self) -> dict[str, object]:
        stored = self.database.get_transfer_settings() or {}
        try:
            value = int(stored.get("max_concurrent_transfers", DEFAULT_MAX_CONCURRENT_TRANSFERS))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = DEFAULT_MAX_CONCURRENT_TRANSFERS
        value = min(_MAX_TRANSFER_CONCURRENCY_CAP, max(1, value))
        return {"max_concurrent_transfers": value}

    async def update_settings(
        self, *, max_concurrent_transfers: int | None = None
    ) -> dict[str, object]:
        settings = await self.get_settings()
        if max_concurrent_transfers is not None:
            try:
                value = int(max_concurrent_transfers)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"max_concurrent_transfers 无效: {max_concurrent_transfers}") from exc
            if not 1 <= value <= _MAX_TRANSFER_CONCURRENCY_CAP:
                raise ValueError(
                    f"max_concurrent_transfers 必须在 1-{_MAX_TRANSFER_CONCURRENCY_CAP} 之间"
                )
            settings["max_concurrent_transfers"] = value
        self.database.set_transfer_settings(settings)
        await self._record_operation(
            action="transfer_settings_updated",
            title="更新传输设置",
            metadata=dict(settings),
        )
        await self.events.publish("transfer_settings_updated", dict(settings))
        await self._dispatch()
        return settings

    # ---------- 内部：路由解析辅助 ----------

    def _matrix_snapshot(self) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
        links = {
            (str(link["from_node_id"]), str(link["to_node_id"])): str(link["status"])
            for link in self.database.list_node_links()
        }
        auths = {
            str(node["id"]): str(node["auth_method"]) for node in self.database.list_nodes()
        }
        return links, auths

    async def _resolve_with_probes(
        self,
        src: str,
        dst: str,
        resolution: dict[str, object],
        *,
        probe_unknown: bool,
    ) -> str:
        """auto 路由：推荐项有 unknown 边且允许探测时逐边探测后重解析（总探测边 ≤4）。"""
        probed: set[tuple[str, str]] = set()
        while True:
            recommended = resolution["recommended"]
            if recommended is None:
                break
            cand = next(
                c for c in resolution["candidates"] if c["route"] == recommended  # type: ignore[union-attr]
            )
            pending = [
                (str(e[0]), str(e[1]))
                for e in cand["requires_probe"]  # type: ignore[union-attr]
                if (str(e[0]), str(e[1])) not in probed
            ]
            if not pending or not probe_unknown or len(probed) >= 4:
                break
            for edge in pending:
                if len(probed) >= 4:
                    break
                probed.add(edge)
                with contextlib.suppress(ValueError):
                    await self.nodes.probe_edge(edge[0], edge[1])
            links, auths = self._matrix_snapshot()
            resolution = resolve_route(src, dst, links, auths)
        recommended = resolution["recommended"]
        if recommended is None:
            reasons = [
                f"{cand['route']}: " + "；".join(str(r) for r in cand["reasons"])  # type: ignore[union-attr]
                for cand in resolution["candidates"]  # type: ignore[union-attr]
            ]
            raise ValueError("没有可用的传输路由。" + " | ".join(reasons))
        # 仍有 unknown 边（探测不可达/被禁用探测）：按推荐项尝试，失败交给运行时降级
        return str(recommended)

    # ---------- 内部：派发与执行 ----------

    async def _dispatch(self) -> None:
        if self._shutting_down:
            return
        async with self._dispatch_lock:
            settings = await self.get_settings()
            max_concurrent = int(settings["max_concurrent_transfers"])  # type: ignore[arg-type]
            active = self.database.list_active_transfer_jobs()
            slots = max_concurrent - sum(1 for job in active if job["status"] == "running")
            for job in (job for job in active if job["status"] == "pending"):
                if slots <= 0:
                    break
                if await self._launch_job(str(job["id"])):
                    slots -= 1

    async def _launch_job(self, job_id: str) -> bool:
        job = self.database.get_transfer_job(job_id)
        if job is None or job["status"] != "pending" or job_id in self._handles:
            return False
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"transfer_{job_id}.log"
        job = self.database.update_transfer_job(
            job_id,
            status="running",
            started_at=utc_now_iso(),
            log_path=str(log_path),
            error=None,
            error_code=None,
        )
        self._handles[job_id] = _JobHandle(job_id=job_id)
        # 日志头：路由 + 端点 + 节点快照（快照已脱敏，不含密码）
        self._log_job(job_id, f"传输任务 {job_id} 启动")
        self._log_job(job_id, f"路由: {job['route']}（{job['route_resolved_by']}）")
        self._log_job(
            job_id,
            f"源: {job['src_node_id']}:{job['src_path']} → 目标: {job['dst_node_id']}:{job['dst_path']}",
        )
        self._log_job(
            job_id, "节点快照: " + json.dumps(job["node_snapshot"], ensure_ascii=False)
        )
        await self.events.publish("transfer_started", {"job_id": job_id, "job": job})
        watcher = asyncio.create_task(self._run_job(job_id), name=f"transfer-{job_id[:8]}")
        self._watchers[job_id] = watcher
        return True

    async def _run_job(self, job_id: str) -> None:
        try:
            job = self.database.get_transfer_job(job_id)
            if job is None:
                return
            route = str(job["route"])
            attempted: list[str] = []
            fallback_used = False
            while True:
                attempted.append(route)
                attempt_started = utc_now_iso()
                try:
                    src_auth = self.nodes.resolve_auth(str(job["src_node_id"]))
                    dst_auth = self.nodes.resolve_auth(str(job["dst_node_id"]))
                except ValueError as exc:
                    await self._finalize(
                        job_id, status="failed", error=str(exc), error_code="node_missing"
                    )
                    return
                self._log_job(job_id, f"使用路由 {route} 启动传输")
                result = await self._attempt_route(job, route, src_auth, dst_auth)
                with contextlib.suppress(ValueError):
                    self.database.append_transfer_route_attempt(
                        job_id,
                        {
                            "route": route,
                            "started_at": attempt_started,
                            "finished_at": utc_now_iso(),
                            "exit_code": result.exit_code,
                            "error_code": result.error_code,
                            "error": result.error,
                            "bridge_port": result.listen_port,
                        },
                    )
                if result.status == "succeeded":
                    await self._finalize(
                        job_id,
                        status="succeeded",
                        exit_code=result.exit_code,
                        bytes_transferred=result.bytes_transferred,
                    )
                    return
                if result.status == "cancelled":
                    await self._finalize(
                        job_id,
                        status="cancelled",
                        exit_code=result.exit_code,
                        error="已取消",
                    )
                    return
                handle = self._handles.get(job_id)
                next_route = None
                fast_fail = (
                    result.duration_seconds < FAST_FAIL_SECONDS
                    and not result.bytes_transferred
                )
                if (
                    not fallback_used
                    and str(job["route_resolved_by"]) == "auto"
                    and fast_fail
                    and result.error_code in _CONNECTION_ERROR_CODES
                    and not (handle and handle.cancel_requested)
                    and not self._shutting_down
                ):
                    next_route = await self._select_fallback_route(
                        job, route, attempted, result
                    )
                if next_route is None:
                    await self._finalize(
                        job_id,
                        status="failed",
                        exit_code=result.exit_code,
                        error=result.error,
                        error_code=result.error_code,
                        bytes_transferred=result.bytes_transferred,
                    )
                    return
                fallback_used = True
                job = self.database.update_transfer_job(job_id, route=next_route)
                self._log_job(
                    job_id,
                    f"路由 {route} 快速失败（{result.error_code}），自动降级到 {next_route}",
                )
                await self._record_operation(
                    level="warning",
                    action="route_fallback",
                    entity_id=job_id,
                    title=f"传输路由降级: {route} → {next_route}",
                    detail=result.error,
                    metadata={"job_id": job_id, "from": route, "to": next_route},
                )
                route = next_route
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — watcher 兜底，必须收敛为 failed
            LOGGER.exception("传输任务 %s 执行异常", job_id)
            with contextlib.suppress(Exception):
                await self._finalize(
                    job_id, status="failed", error=f"内部错误: {exc}", error_code="internal_error"
                )
        finally:
            self._handles.pop(job_id, None)
            self._watchers.pop(job_id, None)
            with contextlib.suppress(Exception):
                await self._dispatch()

    async def _select_fallback_route(
        self,
        job: dict[str, object],
        failed_route: str,
        attempted: list[str],
        result: _AttemptResult,
    ) -> str | None:
        """快速失败后的运行时降级：回写矩阵 → 重解析 → 取下一个未尝试的可行候选。"""
        src = str(job["src_node_id"])
        dst = str(job["dst_node_id"])
        edge = _blamed_edge(failed_route, src, dst, result.error_code or "")
        if edge is not None:
            link = self.database.upsert_node_link(
                from_node_id=edge[0],
                to_node_id=edge[1],
                status="failed",
                latency_ms=None,
                last_error=f"传输任务失败回写: {result.error}",
                probe_method="transfer",
            )
            await self.events.publish(
                "node_link_updated",
                {"from_node_id": edge[0], "to_node_id": edge[1], "link": link},
            )
        links, auths = self._matrix_snapshot()
        resolution = resolve_route(src, dst, links, auths)
        for cand in resolution["candidates"]:  # type: ignore[union-attr]
            if cand["route"] in attempted or not cand["feasible"]:
                continue
            return str(cand["route"])
        return None

    async def _attempt_route(
        self,
        job: dict[str, object],
        route: str,
        src_auth: ResolvedAuth,
        dst_auth: ResolvedAuth,
    ) -> _AttemptResult:
        job_id = str(job["id"])
        src_path = str(job["src_path"])
        dst_path = str(job["dst_path"])
        rsync_args = [str(a) for a in job["rsync_args"]]  # type: ignore[union-attr]
        delete_extras = bool(job["delete_extras"])
        dry_run = bool(job["dry_run"])
        handle = self._handles[job_id]
        if handle.cancel_requested:
            return _AttemptResult(status="cancelled")

        common = {
            "rsync_args": rsync_args,
            "delete_extras": delete_extras,
            "dry_run": dry_run,
        }
        via: ResolvedAuth | None = None
        peer: ResolvedAuth | None = None
        direction: Literal["push", "pull"] = "push"
        bridged = False
        cmd: TransferCommand | None = None
        try:
            if route == "local":
                cmd = build_local_rsync(
                    src_path, dst_path, rsync_binary=self.rsync_binary, **common
                )
            elif route == "direct_from_src" and src_auth.is_local:
                cmd = build_controller_rsync(
                    direction="push",
                    remote=dst_auth,
                    local_path=src_path,
                    remote_path=dst_path,
                    known_hosts_path=self.nodes.known_hosts_path(),
                    rsync_binary=self.rsync_binary,
                    ssh_binary=self.ssh_binary,
                    sshpass_binary=self.sshpass_binary,
                    **common,
                )
            elif route == "direct_from_dst" and dst_auth.is_local:
                cmd = build_controller_rsync(
                    direction="pull",
                    remote=src_auth,
                    local_path=dst_path,
                    remote_path=src_path,
                    known_hosts_path=self.nodes.known_hosts_path(),
                    rsync_binary=self.rsync_binary,
                    ssh_binary=self.ssh_binary,
                    sshpass_binary=self.sshpass_binary,
                    **common,
                )
            elif route == "direct_from_src":
                via, peer, direction = src_auth, dst_auth, "push"
            elif route == "direct_from_dst":
                via, peer, direction = dst_auth, src_auth, "pull"
            elif route == "bridged_push":
                via, peer, direction, bridged = src_auth, dst_auth, "push", True
            elif route == "bridged_pull":
                via, peer, direction, bridged = dst_auth, src_auth, "pull", True
            else:
                raise ValueError(f"未知路由: {route}")
        except ValueError as exc:
            return _AttemptResult(status="failed", error=str(exc), error_code="route_infeasible")

        if cmd is not None:
            # 本机直跑 / 主控直连：无哨兵，直接进入 transferring 相位
            spawn = await self._spawn_and_watch(job_id, cmd, has_sentinel=False)
            return self._spawn_to_attempt(job_id, spawn, listen_port=None)

        # 远端发起 / 桥接：需要每 job 临时 ssh-agent 持有 peer 密钥
        assert via is not None and peer is not None
        if peer.auth_method != "key" or not peer.key_path:
            return _AttemptResult(
                status="failed",
                error=f"路由 {route} 要求对端节点 {peer.name} 使用密钥认证"
                "（密码会暴露在发起端进程列表）",
                error_code="route_infeasible",
            )
        try:
            agent = await self._start_job_agent(job_id, peer.key_path)
        except ValueError as exc:
            return _AttemptResult(status="failed", error=str(exc), error_code="agent_failed")
        handle.agent = agent
        try:
            hostkey_lines = self.nodes.lookup_host_key_lines(
                str(peer.host), peer.port, host_key_alias(peer.node_id)
            )
            if bridged:
                return await self._run_bridged(
                    job_id,
                    via=via,
                    peer=peer,
                    direction=direction,
                    src_path=src_path,
                    dst_path=dst_path,
                    hostkey_lines=hostkey_lines,
                    agent_sock=agent.sock,
                    **common,
                )
            cmd = build_remote_initiated(
                via=via,
                peer=peer,
                direction=direction,
                src_path=src_path,
                dst_path=dst_path,
                known_hosts_path=self.nodes.known_hosts_path(),
                agent_sock=agent.sock,
                job_id=job_id,
                peer_hostkey_lines=hostkey_lines,
                ssh_binary=self.ssh_binary,
                sshpass_binary=self.sshpass_binary,
                **common,
            )
            spawn = await self._spawn_and_watch(job_id, cmd, has_sentinel=True)
            return self._spawn_to_attempt(job_id, spawn, listen_port=None)
        finally:
            self._stop_job_agent(job_id, agent)
            handle.agent = None

    async def _run_bridged(
        self,
        job_id: str,
        *,
        via: ResolvedAuth,
        peer: ResolvedAuth,
        direction: Literal["push", "pull"],
        src_path: str,
        dst_path: str,
        hostkey_lines: list[str],
        agent_sock: str,
        rsync_args: list[str],
        delete_extras: bool,
        dry_run: bool,
    ) -> _AttemptResult:
        """桥接端口循环：forward 被拒/inner 连接类失败换端口 ≤3 次，末次加 -v 探测禁转发。"""
        handle = self._handles[job_id]
        result = _AttemptResult(status="failed", error="桥接未启动", error_code="internal_error")
        for attempt in range(1, MAX_PORT_ATTEMPTS + 1):
            if handle.cancel_requested:
                return _AttemptResult(status="cancelled")
            port = self._reserve_port(via.node_id)
            verbose = attempt == MAX_PORT_ATTEMPTS
            try:
                cmd = build_bridged_command(
                    via=via,
                    peer=peer,
                    direction=direction,
                    src_path=src_path,
                    dst_path=dst_path,
                    listen_port=port,
                    peer_hostkey_lines=hostkey_lines,
                    known_hosts_path=self.nodes.known_hosts_path(),
                    agent_sock=agent_sock,
                    job_id=job_id,
                    rsync_args=rsync_args,
                    delete_extras=delete_extras,
                    dry_run=dry_run,
                    verbose=verbose,
                    ssh_binary=self.ssh_binary,
                    sshpass_binary=self.sshpass_binary,
                )
            except ValueError as exc:
                self._release_port(via.node_id, port)
                return _AttemptResult(
                    status="failed", error=str(exc), error_code="peer_hostkey_missing"
                )
            try:
                spawn = await self._spawn_and_watch(job_id, cmd, has_sentinel=True)
            finally:
                self._release_port(via.node_id, port)
            result = self._spawn_to_attempt(job_id, spawn, listen_port=port)
            if result.status != "failed":
                return result
            retry = False
            if result.error_code is not None and not handle.cancel_requested:
                _, _, retry = classify_transfer_failure(
                    spawn.phase, spawn.exit_code, spawn.stderr_tail, port
                )
            if retry and attempt < MAX_PORT_ATTEMPTS:
                self._log_job(
                    job_id,
                    f"端口 {port} 转发被拒或隧道连接异常（第 {attempt}/{MAX_PORT_ATTEMPTS} 次），换端口重试",
                )
                continue
            if retry and result.error_code == "bridge_forward_denied":
                # 末次 -v 探测：确定性判定 sshd 禁转发 vs 启发式耗尽
                if "Server has disabled port forwarding." in spawn.stderr_tail:
                    message = (
                        f"发起端 {via.name} 的 sshd 已禁用端口转发"
                        "（AllowTcpForwarding no / DisableForwarding yes）。"
                        "可联系管理员放开转发，或手动两段传输（源→本机→目标）。"
                    )
                else:
                    message = (
                        f"发起端 {via.name} 的 sshd 连续 {MAX_PORT_ATTEMPTS} 个随机端口均拒绝转发，"
                        "疑似禁用端口转发。可联系管理员放开，或手动两段传输（源→本机→目标）。"
                    )
                result = _AttemptResult(
                    status="failed",
                    exit_code=result.exit_code,
                    error_code="bridge_forward_denied",
                    error=message,
                    duration_seconds=result.duration_seconds,
                    bytes_transferred=result.bytes_transferred,
                    listen_port=port,
                )
            return result
        return result

    def _spawn_to_attempt(
        self, job_id: str, spawn: _SpawnResult, *, listen_port: int | None
    ) -> _AttemptResult:
        handle = self._handles.get(job_id)
        if handle is not None and handle.cancel_requested:
            return _AttemptResult(
                status="cancelled",
                exit_code=spawn.exit_code,
                duration_seconds=spawn.duration_seconds,
                bytes_transferred=spawn.bytes_transferred,
                listen_port=listen_port,
            )
        if spawn.exit_code == 0:
            return _AttemptResult(
                status="succeeded",
                exit_code=0,
                duration_seconds=spawn.duration_seconds,
                bytes_transferred=spawn.bytes_transferred,
                listen_port=listen_port,
            )
        if spawn.timed_out:
            error_code = "bridge_timeout"
            error = f"建立连接超时（{int(CONNECTING_TIMEOUT_SECONDS)} 秒未就绪）。"
        else:
            error_code, error, _retry = classify_transfer_failure(
                spawn.phase, spawn.exit_code, spawn.stderr_tail, listen_port
            )
        self._log_job(job_id, f"传输尝试失败: [{error_code}] {error}")
        return _AttemptResult(
            status="failed",
            exit_code=spawn.exit_code,
            error_code=error_code,
            error=error,
            duration_seconds=spawn.duration_seconds,
            bytes_transferred=spawn.bytes_transferred,
            listen_port=listen_port,
        )

    # ---------- 内部：进程监督与相位机 ----------

    async def _spawn_and_watch(
        self, job_id: str, cmd: TransferCommand, *, has_sentinel: bool
    ) -> _SpawnResult:
        """启动传输进程并监督：stdout/stderr 分流双 reader + 哨兵相位机 + 45s 建桥超时。"""
        handle = self._handles[job_id]
        initial_phase = "connecting" if has_sentinel else "transferring"
        env = os.environ.copy()
        env.update(cmd.env_extra)
        env["EXPSCHED_JOB_ID"] = job_id  # 重启恢复时经 /proc/{pid}/environ 识别归属
        self._log_job(job_id, f"$ {cmd.redacted}")
        started = time.monotonic()
        try:
            process = subprocess.Popen(  # noqa: S603 — argv 列表，不经 shell
                cmd.argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except (FileNotFoundError, PermissionError) as exc:
            detail = f"无法执行命令 {cmd.argv[0]}: {exc}"
            self._log_job(job_id, detail)
            return _SpawnResult(
                exit_code=127,
                phase=initial_phase,
                stderr_tail=detail,
                timed_out=False,
                bytes_transferred=None,
                duration_seconds=0.0,
            )
        handle.process = process
        if handle.cancel_requested:
            # 取消请求落在 spawn 前的准备窗口（如 ssh-agent 启动期间）：立即对新进程组走取消梯
            self._log_job(job_id, "进程启动前已收到取消请求，立即终止传输进程")
            self._begin_cancel_ladder(handle)
        state = _StreamState(job_id=job_id, phase=initial_phase)
        with contextlib.suppress(ValueError):
            self.database.update_transfer_job(
                job_id, pid=process.pid, phase=initial_phase, bridge_port=cmd.listen_port
            )
        await self._publish_progress(state)
        loop = asyncio.get_running_loop()
        sentinel_event = asyncio.Event()
        stdout_task = asyncio.create_task(
            self._consume_stdout(job_id, process.stdout, state, sentinel_event)
        )
        stderr_task = asyncio.create_task(self._consume_stderr(job_id, process.stderr, state))
        wait_task = loop.run_in_executor(self._executor, process.wait)
        if has_sentinel:
            sentinel_task = asyncio.create_task(sentinel_event.wait())
            done, _pending = await asyncio.wait(
                {sentinel_task, wait_task},
                timeout=CONNECTING_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done and process.poll() is None:
                # connecting 相位 45s 无哨兵未退出 → 杀进程组报建立连接超时
                state.timed_out = True
                self._log_job(
                    job_id,
                    f"建立连接超时（{int(CONNECTING_TIMEOUT_SECONDS)} 秒未就绪），终止进程组",
                )
                self._signal_group(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(wait_task), timeout=3.0)
                except (TimeoutError, asyncio.TimeoutError):
                    self._signal_group(process.pid, signal.SIGKILL)
            sentinel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sentinel_task
        exit_code = await wait_task
        await stdout_task
        await stderr_task
        handle.process = None
        with contextlib.suppress(ValueError):
            self.database.update_transfer_job(job_id, pid=None)
        return _SpawnResult(
            exit_code=exit_code,
            phase=state.phase,
            stderr_tail=state.stderr_tail(),
            timed_out=state.timed_out,
            bytes_transferred=state.bytes_transferred,
            duration_seconds=time.monotonic() - started,
        )

    async def _consume_stdout(
        self,
        job_id: str,
        stream: object,
        state: _StreamState,
        sentinel_event: asyncio.Event,
    ) -> None:
        """stdout reader：按 \\r/\\n 切分，解析哨兵与 progress2 进度行。"""
        assert stream is not None
        loop = asyncio.get_running_loop()
        buffer = b""
        try:
            while True:
                # read1: 有数据即返回（read 会阻塞等满 4096 字节，拖慢进度推送）；
                # 跑在专属线程池里，长阻塞不饿死全局默认池
                chunk = await loop.run_in_executor(self._executor, stream.read1, 4096)  # type: ignore[attr-defined]
                if not chunk:
                    break
                buffer += chunk
                parts = re.split(rb"[\r\n]", buffer)
                buffer = parts.pop()
                for raw in parts:
                    await self._handle_stdout_line(job_id, state, sentinel_event, raw)
            if buffer:
                await self._handle_stdout_line(job_id, state, sentinel_event, buffer)
        except Exception:  # noqa: BLE001 — reader 不允许把异常抛回 watcher
            LOGGER.warning("读取传输任务 %s 的输出失败", job_id, exc_info=True)

    async def _handle_stdout_line(
        self,
        job_id: str,
        state: _StreamState,
        sentinel_event: asyncio.Event,
        raw: bytes,
    ) -> None:
        line = raw.decode("utf-8", errors="replace")
        stripped = line.strip()
        if not stripped:
            return
        if state.phase == "connecting" and BRIDGE_SENTINEL in stripped:
            state.phase = "transferring"
            sentinel_event.set()
            with contextlib.suppress(ValueError):
                self.database.update_transfer_job(job_id, phase="transferring")
            self._log_job(job_id, "远端会话就绪，进入传输阶段")
            await self._publish_progress(state)
            return
        progress = parse_progress_line(line)
        if progress is None:
            self._log_job(job_id, stripped)
            return
        state.bytes_transferred = int(progress["bytes"])  # type: ignore[arg-type]
        percent = float(progress["percent"])  # type: ignore[arg-type]
        state.percent = percent
        if progress["xfr"] is not None:
            state.files_transferred = int(progress["xfr"])  # type: ignore[arg-type]
        now = time.monotonic()
        # 进度节流：percent 变化 ≥1 或距上次 ≥1s 才落库 + 推送
        if (
            abs(percent - state.last_update_percent) >= 1.0
            or now - state.last_update_time >= 1.0
        ):
            state.last_update_percent = percent
            state.last_update_time = now
            with contextlib.suppress(ValueError):
                self.database.update_transfer_job(
                    job_id,
                    progress_percent=percent,
                    bytes_transferred=state.bytes_transferred,
                    transfer_rate=str(progress["rate"]),
                    eta=str(progress["eta"]),
                    files_transferred=state.files_transferred,
                )
            await self._publish_progress(state, rate=str(progress["rate"]), eta=str(progress["eta"]))
        # 进度行每 5s 记一帧日志，避免刷爆日志文件
        if now - state.last_progress_log_time >= PROGRESS_LOG_INTERVAL_SECONDS:
            state.last_progress_log_time = now
            self._log_job(job_id, f"进度: {stripped}")

    async def _consume_stderr(self, job_id: str, stream: object, state: _StreamState) -> None:
        """stderr reader：全量写日志（[stderr] 前缀）+ 尾部 8KB 环形缓冲供失败分类。"""
        assert stream is not None
        loop = asyncio.get_running_loop()
        buffer = b""
        try:
            while True:
                # read1: 有数据即返回（read 会阻塞等满 4096 字节，拖慢进度推送）；
                # 跑在专属线程池里，长阻塞不饿死全局默认池
                chunk = await loop.run_in_executor(self._executor, stream.read1, 4096)  # type: ignore[attr-defined]
                if not chunk:
                    break
                state.stderr_buf += chunk
                if len(state.stderr_buf) > STDERR_TAIL_BYTES:
                    del state.stderr_buf[: len(state.stderr_buf) - STDERR_TAIL_BYTES]
                buffer += chunk
                parts = re.split(rb"[\r\n]", buffer)
                buffer = parts.pop()
                for raw in parts:
                    text = raw.decode("utf-8", errors="replace").rstrip()
                    if text:
                        self._log_job(job_id, f"[stderr] {text}")
            if buffer.strip():
                self._log_job(
                    job_id, "[stderr] " + buffer.decode("utf-8", errors="replace").rstrip()
                )
        except Exception:  # noqa: BLE001
            LOGGER.warning("读取传输任务 %s 的 stderr 失败", job_id, exc_info=True)

    async def _publish_progress(
        self, state: _StreamState, *, rate: str | None = None, eta: str | None = None
    ) -> None:
        await self.events.publish(
            "transfer_progress",
            {
                "job_id": state.job_id,
                "percent": state.percent,
                "bytes": state.bytes_transferred,
                "rate": rate,
                "eta": eta,
                "phase": state.phase,
            },
        )

    def _begin_cancel_ladder(self, handle: _JobHandle) -> bool:
        """对在跑进程组发 SIGINT 并启动取消梯；无在跑进程或已有取消梯在途时返回 False。"""
        process = handle.process
        if process is None or process.poll() is not None:
            return False
        if handle.cancel_ladder is not None and not handle.cancel_ladder.done():
            return False
        self._signal_group(process.pid, signal.SIGINT)
        handle.cancel_ladder = asyncio.create_task(
            self._stop_ladder(handle.job_id), name=f"transfer-cancel-{handle.job_id[:8]}"
        )
        return True

    async def _stop_ladder(self, job_id: str) -> None:
        """取消梯后段：SIGINT 已发，5s → SIGTERM，再 5s → SIGKILL。"""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            await asyncio.sleep(5)
            handle = self._handles.get(job_id)
            if handle is None:
                return
            process = handle.process
            if process is None or process.poll() is not None:
                return
            self._signal_group(process.pid, sig)

    # ---------- 内部：桥接端口与临时 agent ----------

    def _reserve_port(self, via_node_id: str) -> int:
        used = self._ports_in_use.setdefault(via_node_id, set())
        span = BRIDGE_PORT_RANGE[1] - BRIDGE_PORT_RANGE[0]
        for _ in range(64):
            port = BRIDGE_PORT_RANGE[0] + secrets.randbelow(span)
            if port not in used:
                used.add(port)
                return port
        raise ValueError("无法分配桥接监听端口（候选端口均在使用中）")

    def _release_port(self, via_node_id: str, port: int) -> None:
        self._ports_in_use.get(via_node_id, set()).discard(port)

    async def _start_job_agent(self, job_id: str, key_path: str) -> _JobAgent:
        """每 job 临时 ssh-agent：只装 peer 一把密钥，跨端口重试/降级复用，终态销毁。"""
        run_dir = self.state_dir / "run"
        _ensure_private_dir(run_dir)
        sock = run_dir / f"agent-{job_id}.sock"
        if len(str(sock).encode()) > _MAX_SOCKET_PATH_BYTES:
            # Unix socket 路径上限 108 字节：state_dir 过深时回落 /tmp（属主/权限校验防抢占）
            fallback = _fallback_agent_dir()
            _ensure_private_dir(fallback)
            sock = fallback / f"agent-{job_id}.sock"
        with contextlib.suppress(OSError):
            sock.unlink()
        rc, stdout, stderr = await self._run_local([self.ssh_agent_binary, "-a", str(sock)])
        # agent 可能已 daemonize（含 _run_local 超时杀父进程的情形）：
        # 从这里起任何失败路径都必须回收 agent 进程与 socket，防止常驻泄漏
        agent: _JobAgent | None = None
        try:
            if rc != 0:
                raise ValueError(
                    f"无法启动传输任务的临时 ssh-agent: {(stderr or '').strip()[:200]}"
                )
            match = _SSH_AGENT_PID_RE.search(stdout)
            if match is None:
                raise ValueError("无法解析临时 ssh-agent 的进程号")
            agent = _JobAgent(pid=int(match.group(1)), sock=str(sock))
            with contextlib.suppress(ValueError):
                self.database.update_transfer_job(job_id, agent_pid=agent.pid)
            rc, _stdout, stderr = await self._run_local(
                [self.ssh_add_binary, key_path], env_extra={"SSH_AUTH_SOCK": agent.sock}
            )
            if rc != 0:
                raise ValueError(
                    f"加载对端密钥到临时 agent 失败（密钥可能带 passphrase）: {stderr.strip()[:200]}"
                )
            return agent
        except BaseException:
            if agent is not None:
                self._stop_job_agent(job_id, agent)
            else:
                # pid 未知（启动失败/解析失败）：按 sock 路径在 /proc 中兜底回收
                for orphan_pid in _find_pids_by_cmdline_arg(str(sock)):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(orphan_pid, signal.SIGTERM)
                with contextlib.suppress(OSError):
                    sock.unlink()
            raise

    def _stop_job_agent(self, job_id: str, agent: _JobAgent) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(agent.pid, signal.SIGTERM)
        with contextlib.suppress(OSError):
            os.unlink(agent.sock)
        with contextlib.suppress(ValueError):
            self.database.update_transfer_job(job_id, agent_pid=None)

    async def _run_local(
        self,
        argv: list[str],
        *,
        env_extra: dict[str, str] | None = None,
        timeout_seconds: float = LOCAL_COMMAND_TIMEOUT_SECONDS,
    ) -> tuple[int | None, str, str]:
        env = os.environ.copy()
        if env_extra:
            env.update(env_extra)
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
            return 127, "", f"无法执行命令 {argv[0]}: {exc}"
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except (TimeoutError, asyncio.TimeoutError):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()
            return None, "", "命令执行超时"
        return (
            process.returncode,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    # ---------- 内部：收尾与杂项 ----------

    async def _finalize(
        self,
        job_id: str,
        *,
        status: str,
        exit_code: int | None = None,
        error: str | None = None,
        error_code: str | None = None,
        bytes_transferred: int | None = None,
    ) -> None:
        fields: dict[str, object] = {
            "status": status,
            "finished_at": utc_now_iso(),
            "exit_code": exit_code,
            "error": error,
            "error_code": error_code,
            "pid": None,
            "agent_pid": None,
        }
        if status == "succeeded":
            fields["progress_percent"] = 100.0
            fields["error"] = None
            fields["error_code"] = None
        if bytes_transferred is not None:
            fields["bytes_transferred"] = bytes_transferred
        try:
            job = self.database.update_transfer_job(job_id, **fields)
        except ValueError:
            return  # 任务已被删除
        label = {"succeeded": "完成", "failed": "失败", "cancelled": "已取消"}.get(status, status)
        self._log_job(job_id, f"传输{label}" + (f"：{error}" if error else ""))
        await self._record_operation(
            level="info" if status in ("succeeded", "cancelled") else "warning",
            action="transfer_finished",
            entity_id=job_id,
            title=f"传输任务{label}: {job.get('name') or job_id}",
            detail=error,
            metadata={"job_id": job_id, "status": status, "exit_code": exit_code,
                      "error_code": error_code, "route": job.get("route")},
        )
        await self.events.publish(
            "transfer_finished", {"job_id": job_id, "status": status, "job": job}
        )

    def _log_job(self, job_id: str, text: str) -> None:
        path = self.log_dir / f"transfer_{job_id}.log"
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except OSError:
            LOGGER.warning("写入传输日志失败: %s", path, exc_info=True)

    def _normalize_path(self, value: str, *, label: str) -> str:
        path = (value or "").strip()
        if not path:
            raise ValueError(f"{label}不能为空")
        if path.startswith("-"):
            raise ValueError(f"{label}不能以 - 开头: {path}")
        return path

    def _signal_group(self, pid: int | None, sig: signal.Signals) -> None:
        if pid is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)

    async def _record_operation(
        self,
        *,
        action: str,
        title: str,
        level: str = "info",
        entity_id: str | None = None,
        detail: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if entity_id is not None:
            metadata = {**(metadata or {}), "job_id": entity_id}
        try:
            log = self.database.add_operation_log(
                level=level,
                source="transfer",
                action=action,
                entity_type="transfer_job",
                entity_id=None,  # 传输任务 id 为 hex 字符串，统一记入 metadata（沿 nodes.py 先例）
                title=title,
                detail=detail,
                metadata=metadata,
            )
        except Exception:  # noqa: BLE001 — 审计失败不阻断主流程
            LOGGER.warning("写入操作日志失败: %s", action, exc_info=True)
            return
        await self.events.publish(
            "operation_log_created", {"log_id": log["id"], "action": action}
        )


# ---------- 模块级辅助 ----------


def _auth_snapshot(auth: ResolvedAuth) -> dict[str, object]:
    """节点连接参数快照（入 job 行，节点编辑不影响在途任务）；密码不入快照。"""
    return {
        "id": auth.node_id,
        "name": auth.name,
        "is_local": auth.is_local,
        "host": auth.host,
        "ssh_port": auth.port,
        "username": auth.username,
        "auth_method": auth.auth_method,
        "has_password": bool(auth.password),
    }


def _proc_env_has(pid: int, marker: str) -> bool:
    """检查 /proc/{pid}/environ 是否含指定 KEY=VALUE 项（防 pid 复用误杀）。"""
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return marker.encode() in data.split(b"\x00")


def _proc_cmdline_contains(pid: int, needle: str) -> bool:
    """检查 /proc/{pid}/cmdline 是否有 argv 项含指定子串（agent 归属校验，防 pid 复用误杀）。"""
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    encoded = needle.encode()
    return any(encoded in part for part in data.split(b"\x00"))


def _find_pids_by_cmdline_arg(arg: str) -> list[int]:
    """在 /proc 中查找 argv 含指定项（逐项精确匹配）的进程，用于按 sock 路径回收孤儿 agent。

    sock 路径含 uuid 任务号且位于本服务私有目录，精确 argv 匹配足以避免误杀。
    """
    needle = arg.encode()
    own_pid = os.getpid()
    pids: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return pids
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == own_pid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmdline.split(b"\x00"):
            pids.append(pid)
    return pids


def _fallback_agent_dir() -> Path:
    """agent socket 路径超长时的回落目录（/tmp 下按 uid 隔离，使用前须经 _ensure_private_dir 校验）。"""
    return _FALLBACK_TMP_ROOT / f"exp-sched-{os.getuid()}"


def _ensure_private_dir(path: Path) -> None:
    """确保目录存在、属主为当前用户且权限为 0700（存放 ssh-agent socket 前的防抢占校验）。

    目录可能被其他用户在共享 /tmp 中预先创建（mkdir(exist_ok=True) 不会失败），
    因此必须显式校验属主；权限收紧失败也不再静默吞掉。
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.stat(path)
    if info.st_uid != os.getuid():
        raise ValueError(
            f"目录 {path} 的属主不是当前用户（可能已被其他用户抢占），"
            "拒绝在其中存放 ssh-agent socket"
        )
    if (info.st_mode & 0o777) != 0o700:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise ValueError(f"无法将目录 {path} 的权限收紧为 0700: {exc}") from exc
