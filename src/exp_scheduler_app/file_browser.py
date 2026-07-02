from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING

from .nodes import build_ssh_command, translate_ssh_error

if TYPE_CHECKING:
    from .nodes import ResolvedAuth


class FileBrowserService:
    """目录浏览服务：列出本地或远端节点的目录内容。

    用于前端目录选择器，支持备份和文件同步场景。
    本地节点直接 os.scandir；远端节点通过 SSH 执行 ls --almost-all。
    """

    def __init__(
        self,
        *,
        node_resolver: Callable[[str], ResolvedAuth],
        known_hosts_path: Path | str,
        ssh_binary: str = "ssh",
        sshpass_binary: str = "sshpass",
    ) -> None:
        self.node_resolver = node_resolver
        self.known_hosts_path = known_hosts_path
        self.ssh_binary = ssh_binary
        self.sshpass_binary = sshpass_binary

    async def list_directory(
        self,
        node_id: str,
        path: str,
    ) -> dict[str, object]:
        """列出指定节点上 path 目录的内容。

        返回 {"path": resolved_path, "entries": [{name, type, size, modified}]}
        type 为 "dir" 或 "file"。
        """
        auth = self.node_resolver(node_id)
        normalized = self._normalize_path(path)
        if auth.is_local:
            return self._list_local(normalized)
        return await self._list_remote(auth, normalized)

    def _normalize_path(self, path: str) -> str:
        path = path.strip()
        if not path or path == "~":
            return str(Path.home())
        if path.startswith("~/"):
            return str(Path.home() / path[2:])
        return path

    def _list_local(self, path: str) -> dict[str, object]:
        p = Path(path)
        if not p.exists():
            raise ValueError(f"路径不存在: {path}")
        if not p.is_dir():
            raise ValueError(f"不是目录: {path}")
        entries: list[dict[str, object]] = []
        try:
            for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if item.name.startswith(".") and item.name not in ("..",):
                    continue
                try:
                    stat = item.stat()
                    entries.append({
                        "name": item.name,
                        "type": "dir" if item.is_dir() else "file",
                        "size": stat.st_size if item.is_file() else 0,
                        "modified": stat.st_mtime,
                    })
                except OSError:
                    continue
        except PermissionError as exc:
            raise ValueError(f"无权限访问目录: {path}") from exc
        return {"path": str(p), "entries": entries}

    async def _list_remote(
        self,
        auth: ResolvedAuth,
        path: str,
    ) -> dict[str, object]:
        ssh_argv, ssh_env = build_ssh_command(
            auth,
            known_hosts_path=self.known_hosts_path,
            remote_command=None,
            ssh_binary=self.ssh_binary,
            sshpass_binary=self.sshpass_binary,
        )
        env = os.environ.copy()
        env.update(ssh_env)
        escaped_path = path.replace("'", "'\\''")
        remote_cmd = (
            f"cd '{escaped_path}' 2>/dev/null && "
            f"for f in * .[!.]* ..?*; do "
            f"  [ -e \"$f\" ] || [ -L \"$f\" ] || continue; "
            f"  if [ -d \"$f\" ]; then echo \"D\\t$f\\t0\\t\"; "
            f"  else sz=$(stat -c %s \"$f\" 2>/dev/null || echo 0); "
            f"       mt=$(stat -c %Y \"$f\" 2>/dev/null || echo 0); "
            f"       echo \"F\\t$f\\t$sz\\t$mt\"; fi; "
            f"done"
        )
        full_argv = [*ssh_argv, remote_cmd]
        try:
            result = subprocess.run(
                full_argv,
                capture_output=True,
                text=True,
                env=env,
                timeout=15,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"远端目录浏览超时（{auth.name}）") from exc
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "No such file or directory" in stderr or "not a directory" in stderr:
                raise ValueError(f"路径不存在或不是目录: {path}")
            raise ValueError(
                f"远端目录浏览失败（{auth.name}）: {translate_ssh_error(stderr, fallback='SSH 连接失败')}"
            )
        entries: list[dict[str, object]] = []
        dirs: list[dict[str, object]] = []
        files: list[dict[str, object]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            kind, name = parts[0], parts[1]
            size = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            modified = float(parts[3]) if len(parts) > 3 and parts[3].replace(".", "").isdigit() else 0
            if not name or name in (".", ".."):
                continue
            entry = {
                "name": name,
                "type": "dir" if kind == "D" else "file",
                "size": size,
                "modified": modified,
            }
            if kind == "D":
                dirs.append(entry)
            else:
                files.append(entry)
        dirs.sort(key=lambda x: x["name"].lower())
        files.sort(key=lambda x: x["name"].lower())
        return {"path": path, "entries": dirs + files}
