from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import socket
import tomllib


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17861
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_GPU_IDLE_MEMORY_MB = 2000
DEFAULT_GPU_IDLE_REQUIRED_CHECKS = 6
DEFAULT_AUTO_RETRY_MAX_RETRIES = 0
DEFAULT_AUTO_RETRY_DELAY_SECONDS = 5
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "exp-scheduler" / "config.toml"
DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "exp-scheduler"


def _detect_server_name() -> str:
    return socket.gethostname() or "unknown-host"


def _detect_server_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            ip = sock.getsockname()[0]
            if ip:
                return ip
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip:
            return ip
    except OSError:
        pass
    return "127.0.0.1"


DEFAULT_SERVER_NAME = _detect_server_name()
DEFAULT_SERVER_IP = _detect_server_ip()


@dataclass(slots=True)
class SchedulerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    server_name: str = DEFAULT_SERVER_NAME
    server_ip: str = DEFAULT_SERVER_IP
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    gpu_idle_memory_mb: int = DEFAULT_GPU_IDLE_MEMORY_MB
    gpu_idle_required_checks: int = DEFAULT_GPU_IDLE_REQUIRED_CHECKS
    auto_retry_max_retries: int = DEFAULT_AUTO_RETRY_MAX_RETRIES
    auto_retry_delay_seconds: int = DEFAULT_AUTO_RETRY_DELAY_SECONDS
    state_dir: Path = DEFAULT_STATE_DIR
    log_dir: Path = DEFAULT_STATE_DIR / "logs"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "scheduler.db"

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.toml"


def _resolve_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def config_from_mapping(data: dict[str, object]) -> SchedulerConfig:
    state_dir = _resolve_path(data.get("state_dir", DEFAULT_STATE_DIR))
    log_dir = _resolve_path(data.get("log_dir", state_dir / "logs"))
    return SchedulerConfig(
        host=str(data.get("host", DEFAULT_HOST)),
        port=int(data.get("port", DEFAULT_PORT)),
        server_name=str(data.get("server_name", DEFAULT_SERVER_NAME)),
        server_ip=str(data.get("server_ip", DEFAULT_SERVER_IP)),
        poll_interval_seconds=float(
            data.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
        ),
        gpu_idle_memory_mb=int(
            data.get("gpu_idle_memory_mb", DEFAULT_GPU_IDLE_MEMORY_MB)
        ),
        gpu_idle_required_checks=int(
            data.get("gpu_idle_required_checks", DEFAULT_GPU_IDLE_REQUIRED_CHECKS)
        ),
        auto_retry_max_retries=int(
            data.get("auto_retry_max_retries", DEFAULT_AUTO_RETRY_MAX_RETRIES)
        ),
        auto_retry_delay_seconds=int(
            data.get("auto_retry_delay_seconds", DEFAULT_AUTO_RETRY_DELAY_SECONDS)
        ),
        state_dir=state_dir,
        log_dir=log_dir,
    )


def default_config_text() -> str:
    return "\n".join(
        [
            f'host = "{DEFAULT_HOST}"',
            f"port = {DEFAULT_PORT}",
            f'server_name = "{DEFAULT_SERVER_NAME}"',
            f'server_ip = "{DEFAULT_SERVER_IP}"',
            f"poll_interval_seconds = {DEFAULT_POLL_INTERVAL_SECONDS}",
            f"gpu_idle_memory_mb = {DEFAULT_GPU_IDLE_MEMORY_MB}",
            f"gpu_idle_required_checks = {DEFAULT_GPU_IDLE_REQUIRED_CHECKS}",
            f"auto_retry_max_retries = {DEFAULT_AUTO_RETRY_MAX_RETRIES}",
            f"auto_retry_delay_seconds = {DEFAULT_AUTO_RETRY_DELAY_SECONDS}",
            f'state_dir = "{DEFAULT_STATE_DIR}"',
            f'log_dir = "{DEFAULT_STATE_DIR / "logs"}"',
            "",
        ]
    )


def ensure_directories(config: SchedulerConfig) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)


def write_default_config(config_path: Path, *, force: bool = False) -> Path:
    config_path = _resolve_path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists() and not force:
        return config_path
    config_path.write_text(default_config_text(), encoding="utf-8")
    return config_path


def load_config(config_path: Path | None = None) -> SchedulerConfig:
    path = _resolve_path(config_path or DEFAULT_CONFIG_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {path}。请先运行 `exp-scheduler init`。"
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    config = config_from_mapping(data)
    ensure_directories(config)
    return config


def init_config(config_path: Path | None = None, *, force: bool = False) -> SchedulerConfig:
    path = write_default_config(config_path or DEFAULT_CONFIG_PATH, force=force)
    config = load_config(path)
    ensure_directories(config)
    return config


def check_port_available(host: str, port: int) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()
    return True, "ok"
