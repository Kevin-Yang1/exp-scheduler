from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import uvicorn

from .config import DEFAULT_CONFIG_PATH, check_port_available, init_config, load_config
from .database import Database
from .tmux_utils import ensure_tmux_installed, has_passwordless_sudo, detect_package_manager
from .web import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GPU experiment task scheduler")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"配置文件路径，默认: {DEFAULT_CONFIG_PATH}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化配置和状态目录")
    init_parser.add_argument("--force", action="store_true", help="覆盖已有配置文件")

    subparsers.add_parser("serve", help="启动 Web 服务")
    subparsers.add_parser("doctor", help="检查运行环境")
    return parser


def run_init(config_path: Path, *, force: bool) -> int:
    config = init_config(config_path, force=force)
    database = Database(config.db_path)
    database.init()
    print(f"配置文件: {config_path.expanduser().resolve()}")
    print(f"状态目录: {config.state_dir}")
    print(f"日志目录: {config.log_dir}")
    print(f"数据库: {config.db_path}")
    print(f"服务器名称: {config.server_name}")
    print(f"服务器地址: {config.server_ip}")
    print(f"检测间隔: {config.poll_interval_seconds}s")
    print(f"连续检测次数: {config.gpu_idle_required_checks}")
    auto_restore = (
        f"{config.auto_restore_idle_gpu_seconds:g}s"
        if config.auto_restore_idle_gpu_seconds
        else "关闭"
    )
    print(f"GPU空闲自动恢复: {auto_restore}")
    print(f"自动重试次数: {config.auto_retry_max_retries}")
    print(f"自动重试延迟: {config.auto_retry_delay_seconds}s")
    return 0


def _state_dir_fstype(path: Path) -> str | None:
    """从 /proc/mounts 找出 path 所在挂载点的文件系统类型（找不到返回 None）。"""
    try:
        resolved = str(path.expanduser().resolve())
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    best_len = -1
    best_fstype: str | None = None
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        # /proc/mounts 中空格/制表符以八进制转义出现
        mountpoint = fields[1].replace("\\040", " ").replace("\\011", "\t")
        normalized = mountpoint.rstrip("/") or "/"
        if (
            resolved == normalized
            or resolved.startswith(normalized + "/")
            or normalized == "/"
        ):
            if len(normalized) > best_len:
                best_len = len(normalized)
                best_fstype = fields[2]
    return best_fstype


def run_doctor(config_path: Path) -> int:
    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        print(exc)
        return 1

    database = Database(config.db_path)
    database.init()

    print(f"配置文件: {config_path.expanduser().resolve()}")
    print(f"状态目录: {config.state_dir}")
    print(f"日志目录: {config.log_dir}")
    print(f"数据库: {config.db_path}")
    print(f"服务器名称: {config.server_name}")
    print(f"服务器地址: {config.server_ip}")
    print(f"检测间隔: {config.poll_interval_seconds}s")
    print(f"连续检测次数: {config.gpu_idle_required_checks}")
    auto_restore = (
        f"{config.auto_restore_idle_gpu_seconds:g}s"
        if config.auto_restore_idle_gpu_seconds
        else "关闭"
    )
    print(f"GPU空闲自动恢复: {auto_restore}")
    print(f"自动重试次数: {config.auto_retry_max_retries}")
    print(f"自动重试延迟: {config.auto_retry_delay_seconds}s")

    nvidia_smi = shutil.which("nvidia-smi")
    print(f"nvidia-smi: {nvidia_smi or 'missing'}")
    if nvidia_smi:
        result = subprocess.run(
            [nvidia_smi, "--query-gpu=index,name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=False,
        )
        status = "ok" if result.returncode == 0 else result.stderr.strip() or "failed"
        print(f"GPU查询: {status}")

    port_ok, message = check_port_available(config.host, config.port)
    print(f"端口 {config.host}:{config.port}: {'ok' if port_ok else message}")

    writable_checks = [
        ("state_dir", config.state_dir),
        ("log_dir", config.log_dir),
        ("db_dir", config.db_path.parent),
    ]
    for label, path in writable_checks:
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_path = path / ".write-test"
            test_path.write_text("ok", encoding="utf-8")
            test_path.unlink()
            print(f"{label} 可写: {path}")
        except OSError as exc:
            print(f"{label} 不可写: {path} ({exc})")
            return 1

    # SSH / rsync 工具链（文件同步与多节点终端依赖）
    for binary in ("ssh", "rsync", "ssh-keygen", "ssh-agent"):
        binary_path = shutil.which(binary)
        if binary_path:
            print(f"{binary}: {binary_path}")
        else:
            print(f"{binary}: missing（警告: 缺少 {binary}，文件同步/远程终端功能将不可用）")
    sshpass_path = shutil.which("sshpass")
    print(f"sshpass: {sshpass_path or '未安装（可选，仅密码认证的节点需要）'}")

    # tmux 检查（持久终端依赖）：缺失时尝试自动安装
    tmux_path = shutil.which("tmux")
    if tmux_path:
        print(f"tmux: {tmux_path}")
    else:
        print("tmux: missing → 尝试自动安装...")
        can_sudo = has_passwordless_sudo()
        is_root = os.geteuid() == 0
        mgr = detect_package_manager()
        if not (is_root or can_sudo):
            print(
                "  ✗ 无法自动安装: 需要 root 或 passwordless sudo 权限。"
                f"请手动安装: {mgr + ' install tmux' if mgr else 'apt install tmux'}"
            )
        elif mgr is None:
            print("  ✗ 无法自动安装: 未检测到支持的包管理器（apt-get/dnf/yum/pacman/zypper）")
        else:
            try:
                tmux_path = ensure_tmux_installed()
                print(f"  ✓ tmux 已安装: {tmux_path}")
            except ValueError as exc:
                print(f"  ✗ {exc}")

    # terminal_log_dir 可写性检查
    try:
        config.terminal_log_dir.mkdir(parents=True, exist_ok=True)
        (config.terminal_log_dir / ".write-test").write_text("ok", encoding="utf-8")
        (config.terminal_log_dir / ".write-test").unlink()
        print(f"terminal_log_dir 可写: {config.terminal_log_dir}")
    except OSError as exc:
        print(f"terminal_log_dir 不可写: {config.terminal_log_dir} ({exc})")
        return 1
    log_fstype = _state_dir_fstype(config.terminal_log_dir)
    if log_fstype:
        print(f"terminal_log_dir 文件系统: {log_fstype}")
        if log_fstype in {"9p", "drvfs", "v9fs"}:
            print(
                "警告: terminal_log_dir 位于 9p/drvfs/v9fs 文件系统，"
                "pipe-pane 日志写入可能不可靠，"
                "请将 terminal_log_dir 移至 Linux 原生分区"
            )

    # state_dir 文件系统：9p/drvfs 上 0600 私钥权限与 ssh-agent socket 不可用
    fstype = _state_dir_fstype(config.state_dir)
    if fstype:
        print(f"state_dir 文件系统: {fstype}")
        if fstype in {"9p", "drvfs", "v9fs"}:
            print(
                "警告: state_dir 位于 9p/drvfs/v9fs 文件系统，"
                "私钥 0600 与 ssh-agent socket 在该文件系统不可用，"
                "请将 state_dir 移至 Linux 原生分区"
            )
    return 0


def _is_loopback_host(host: str) -> bool:
    """判断 host 是否为 loopback 地址（仅允许本地连接）。"""
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback
    except ValueError:
        return host in {"localhost", "localhost.localdomain"}


def run_serve(config_path: Path) -> int:
    config = load_config(config_path)

    # 校验 host 为 loopback（webssh 仅允许本地连接，通过 SSH 端口转发访问）
    if not _is_loopback_host(config.host):
        print(
            f"错误: host={config.host} 不是 loopback 地址。"
            "webssh 终端仅允许本地连接，请将 config.toml 中的 host 设为 127.0.0.1 或 ::1，"
            "通过 SSH 端口转发（ssh -L {port}:127.0.0.1:{port} user@server）访问。"
        )
        return 1

    # 确保 tmux 已安装（持久终端依赖）
    try:
        tmux_path = ensure_tmux_installed()
        print(f"tmux: {tmux_path}")
    except ValueError as exc:
        print(f"tmux: {exc}")
        return 1

    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        return run_init(args.config, force=args.force)
    if args.command == "doctor":
        return run_doctor(args.config)
    if args.command == "serve":
        return run_serve(args.config)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
