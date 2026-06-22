"""多节点 conda 环境清单采集与对比。

- 本地节点复用 profile_discovery 的 conda 发现逻辑（经 asyncio.to_thread）。
- 远程节点通过一次性 ssh 执行 ``bash -lc 'conda --version; conda env list --json'``，
  登录 shell 保证 PATH 中包含 conda。
- 结果只存内存缓存，不落库、不写 operation_logs（纯只读操作）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import signal
import subprocess
import time
from typing import TYPE_CHECKING

from .database import LOCAL_NODE_ID, utc_now_iso
from .profile_discovery import discover_conda_environments

if TYPE_CHECKING:
    from .nodes import NodeRegistryService, ResolvedAuth


# 远程探测脚本：版本行在前，env 清单 JSON 在后；conda 缺失时两段都静默、整体退出码 0
_REMOTE_PROBE_SCRIPT = "conda --version 2>/dev/null; conda env list --json 2>/dev/null || true"
REMOTE_PROBE_COMMAND = "bash -lc " + shlex.quote(_REMOTE_PROBE_SCRIPT)

# conda --version 输出形如 "conda 24.1.2"
_CONDA_VERSION_PATTERN = re.compile(r"^conda\s+([0-9][\w.+-]*)", re.MULTILINE)

# runner 签名：async (auth, command) -> (exit_code, stdout, stderr)
RemoteRunner = Callable[["ResolvedAuth", str], Awaitable[tuple[int, str, str]]]
# 本地发现签名：() -> (候选环境列表, conda 可执行路径)，沿用 discover_conda_environments
LocalProbe = Callable[[], tuple[list[dict[str, object]], Path | None]]


def parse_conda_probe_output(stdout: str) -> tuple[str | None, list[str] | None]:
    """解析远程探测输出，返回 (conda 版本, 环境名列表)。

    未找到可解析的 ``{"envs": [...]}`` JSON 时环境列表为 None。
    环境名规则：路径含 ``/envs/`` 取 basename，否则视为 root prefix 记为 "base"。
    """
    version: str | None = None
    match = _CONDA_VERSION_PATTERN.search(stdout)
    if match:
        version = match.group(1)

    brace_index = stdout.find("{")
    if brace_index < 0:
        return version, None
    try:
        payload = json.loads(stdout[brace_index:])
    except json.JSONDecodeError:
        return version, None
    if not isinstance(payload, dict):
        return version, None

    raw_envs = payload.get("envs")
    if not isinstance(raw_envs, list):
        return version, None

    names: list[str] = []
    seen: set[str] = set()
    for item in raw_envs:
        path = str(item).rstrip("/")
        if not path:
            continue
        name = posixpath.basename(path) if "/envs/" in path else "base"
        if name not in seen:
            names.append(name)
            seen.add(name)
    return version, names


def _tail_text(text: str, limit: int = 400) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return "…" + cleaned[-limit:]


class CondaInventoryService:
    """采集各节点 conda 版本与环境列表，提供带缓存的对比清单。"""

    def __init__(
        self,
        *,
        nodes: NodeRegistryService,
        profile_discovery_provider: LocalProbe | None = None,
        runner: RemoteRunner | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._nodes = nodes
        self._profile_discovery_provider: LocalProbe = (
            profile_discovery_provider or discover_conda_environments
        )
        self._runner: RemoteRunner = runner or self._run_remote
        self._timeout_seconds = timeout_seconds
        self._cache: dict[str, object] | None = None
        self._cache_monotonic: float | None = None
        self._refresh_future: asyncio.Future[dict[str, object]] | None = None

    # ---------------------------------------------------------------- 公开 API

    async def get_inventory(self, refresh: bool = False) -> dict[str, object]:
        """返回全节点 conda 清单；命中缓存且非 refresh 时直接返回缓存。

        refresh=True 或缓存为空时触发实际采集；采集进行中再次调用会复用
        同一个 in-flight future（防抖），不会重复发起 ssh。
        """
        if not refresh and self._cache is not None:
            return self._build_response(self._cache)

        future = self._refresh_future
        if future is None or future.done():
            future = asyncio.ensure_future(self._refresh())
            future.add_done_callback(self._on_refresh_done)
            self._refresh_future = future
        # shield：单个调用方被取消不应中断共享的采集任务
        payload = await asyncio.shield(future)
        return self._build_response(payload)

    # ---------------------------------------------------------------- 采集

    async def _refresh(self) -> dict[str, object]:
        nodes = await self._nodes.list_nodes()
        results = await asyncio.gather(*(self._fetch_node(node) for node in nodes))
        payload: dict[str, object] = {"nodes": list(results), "fetched_at": utc_now_iso()}
        self._cache = payload
        self._cache_monotonic = time.monotonic()
        return payload

    async def _fetch_node(self, node: dict[str, object]) -> dict[str, object]:
        node_id = str(node.get("id") or "")
        entry: dict[str, object] = {
            "node_id": node_id,
            "node_name": str(node.get("name") or node_id),
            "status": "ok",
            "conda_version": None,
            "envs": [],
            "error": None,
            "fetched_at": utc_now_iso(),
        }
        try:
            if node_id == LOCAL_NODE_ID or node.get("is_local"):
                await asyncio.wait_for(self._probe_local(entry), self._timeout_seconds)
            else:
                await asyncio.wait_for(self._probe_remote(node_id, entry), self._timeout_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            entry["status"] = "timeout"
            entry["error"] = f"探测超时（{self._timeout_seconds:g} 秒内未返回）"
            entry["envs"] = []
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单节点失败不影响其他节点
            entry["status"] = "error"
            entry["error"] = f"探测失败: {exc}"
            entry["envs"] = []
        entry["fetched_at"] = utc_now_iso()
        return entry

    async def _probe_local(self, entry: dict[str, object]) -> None:
        candidates, conda_executable = await asyncio.to_thread(self._profile_discovery_provider)
        if conda_executable is None:
            entry["status"] = "no_conda"
            entry["error"] = "本机未安装 conda 或不在 PATH 中"
            return
        entry["envs"] = [
            str(candidate.get("display_name") or "") for candidate in candidates
        ]
        entry["conda_version"] = await asyncio.to_thread(
            _read_local_conda_version, conda_executable
        )

    async def _probe_remote(self, node_id: str, entry: dict[str, object]) -> None:
        auth = self._nodes.resolve_auth(node_id)
        exit_code, stdout, stderr = await self._runner(auth, REMOTE_PROBE_COMMAND)
        if exit_code != 0:
            entry["status"] = "error"
            detail = _tail_text(stderr) or _tail_text(stdout) or "无输出"
            entry["error"] = f"SSH 连接失败 (exit {exit_code}): {detail}"
            return
        version, envs = parse_conda_probe_output(stdout)
        entry["conda_version"] = version
        if envs is None:
            if version is None:
                entry["status"] = "no_conda"
                entry["error"] = "该节点未安装 conda 或不在 PATH 中"
            else:
                entry["status"] = "error"
                entry["error"] = "无法解析 conda env list 输出"
            return
        entry["envs"] = envs

    # ---------------------------------------------------------------- 默认远程 runner

    async def _run_remote(self, auth: ResolvedAuth, command: str) -> tuple[int, str, str]:
        """默认实现：经 ssh 在远程节点执行命令，超时则 killpg 整个进程组。"""
        # 延迟导入避免与 nodes 模块产生加载顺序耦合
        from .nodes import build_ssh_command

        argv, env_extra = build_ssh_command(
            auth,
            known_hosts_path=self._nodes.known_hosts_path(),
            remote_command=command,
        )
        env = {**os.environ, **env_extra}
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), self._timeout_seconds
            )
        except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
            _kill_process_group(process)
            await process.wait()
            raise
        return (
            process.returncode if process.returncode is not None else -1,
            stdout_bytes.decode("utf-8", errors="replace"),
            stderr_bytes.decode("utf-8", errors="replace"),
        )

    # ---------------------------------------------------------------- 内部辅助

    def _build_response(self, payload: dict[str, object]) -> dict[str, object]:
        refreshing = self._refresh_future is not None and not self._refresh_future.done()
        stale_seconds = 0.0
        if self._cache_monotonic is not None:
            stale_seconds = max(0.0, time.monotonic() - self._cache_monotonic)
        nodes = payload.get("nodes")
        return {
            "nodes": [dict(item) for item in nodes] if isinstance(nodes, list) else [],
            "fetched_at": payload.get("fetched_at"),
            "stale_seconds": round(stale_seconds, 1),
            "refreshing": refreshing,
        }

    def _on_refresh_done(self, future: asyncio.Future[dict[str, object]]) -> None:
        if self._refresh_future is future:
            self._refresh_future = None
        if not future.cancelled():
            # 主动取出异常，避免 "exception was never retrieved" 警告
            future.exception()


def _read_local_conda_version(conda_executable: Path) -> str | None:
    """读取本机 conda 版本号；失败时返回 None（不影响环境列表）。"""
    try:
        result = subprocess.run(
            [str(conda_executable), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _CONDA_VERSION_PATTERN.search(result.stdout or "")
    return match.group(1) if match else None


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """尽力杀掉 start_new_session 创建的整个进程组。"""
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
