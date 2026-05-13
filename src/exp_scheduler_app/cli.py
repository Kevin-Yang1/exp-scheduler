from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import uvicorn

from .config import DEFAULT_CONFIG_PATH, check_port_available, init_config, load_config
from .database import Database
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
    return 0


def run_serve(config_path: Path) -> int:
    config = load_config(config_path)
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
