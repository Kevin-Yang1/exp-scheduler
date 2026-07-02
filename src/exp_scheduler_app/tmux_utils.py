from __future__ import annotations

import hashlib
import os
import shutil
import shlex
import subprocess
import time
from pathlib import Path


TMUX_SESSION_PREFIX = "expsched_"
# 远端 pipe-pane 日志目录（由远端 shell 展开到用户家目录）
REMOTE_TERMINAL_LOG_DIR = "$HOME/.local/share/exp-scheduler/terminals"
# tmux 安装超时秒数
TMUX_INSTALL_TIMEOUT = 120


def has_passwordless_sudo() -> bool:
    """检测当前用户是否有 passwordless sudo 权限。"""
    result = subprocess.run(
        ["sudo", "-n", "true"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0


def detect_package_manager() -> str | None:
    """检测系统可用的包管理器，按优先级返回命令名。"""
    managers = ["apt-get", "dnf", "yum", "pacman", "zypper"]
    for mgr in managers:
        if shutil.which(mgr):
            return mgr
    return None


def _build_install_command(manager: str, *, use_sudo: bool) -> list[str]:
    """构造 tmux 安装命令。"""
    if manager == "apt-get":
        args = ["install", "-y", "tmux"]
    elif manager == "dnf":
        args = ["install", "-y", "tmux"]
    elif manager == "yum":
        args = ["install", "-y", "tmux"]
    elif manager == "pacman":
        args = ["-S", "--noconfirm", "tmux"]
    elif manager == "zypper":
        args = ["install", "-y", "tmux"]
    else:
        raise ValueError(f"不支持的包管理器: {manager}")
    if use_sudo:
        return ["sudo", "-n", manager, *args]
    return [manager, *args]


def ensure_tmux_installed() -> str:
    """确保本地已安装 tmux，缺失时尝试自动安装。

    返回 tmux 可执行文件路径。安装失败时抛出 ValueError 并附带提示。
    """
    path = shutil.which("tmux")
    if path:
        return path

    is_root = os.geteuid() == 0
    can_sudo = has_passwordless_sudo() if not is_root else False
    if not is_root and not can_sudo:
        raise ValueError(
            "tmux 未安装且当前用户无 root/passwordless sudo 权限，"
            "请手动安装：apt install tmux / dnf install tmux / yum install tmux"
        )

    manager = detect_package_manager()
    if manager is None:
        raise ValueError(
            "tmux 未安装且未检测到支持的包管理器"
            "（apt-get/dnf/yum/pacman/zypper），请手动安装 tmux"
        )

    cmd = _build_install_command(manager, use_sudo=not is_root)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TMUX_INSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"tmux 安装超时（{TMUX_INSTALL_TIMEOUT}s），请手动安装"
        ) from exc

    if result.returncode != 0:
        raise ValueError(
            f"tmux 自动安装失败（{manager}）: "
            f"{result.stderr.strip() or result.stdout.strip() or '未知错误'}。"
            f"请手动安装：{manager} install tmux"
        )

    path = shutil.which("tmux")
    if not path:
        raise ValueError(
            f"tmux 已通过 {manager} 安装但未在 PATH 中找到，"
            "请检查环境或手动安装"
        )
    return path


def build_remote_tmux_install_script() -> str:
    """构造远端 tmux 检测+安装 shell 脚本（单行，用于 ssh 命令）。

    脚本逻辑：
    1. command -v tmux 成功则直接输出路径
    2. 否则尝试 passwordless sudo 安装
    3. 最后再次 command -v tmux，成功输出路径，失败输出空
    """
    return (
        "command -v tmux 2>/dev/null && exit 0; "
        "SUDO=''; sudo -n true 2>/dev/null && SUDO='sudo -n'; "
        "for m in apt-get dnf yum pacman zypper; do "
        "  if command -v $m >/dev/null 2>&1; then "
        "    case $m in "
        "      apt-get|dnf|yum|zypper) $SUDO $m install -y tmux >/dev/null 2>&1;; "
        "      pacman) $SUDO $m -S --noconfirm tmux >/dev/null 2>&1;; "
        "    esac; "
        "    break; "
        "  fi; "
        "done; "
        "command -v tmux 2>/dev/null"
    )


def ensure_remote_tmux(
    ssh_argv: list[str],
    ssh_env: dict[str, str],
    *,
    node_name: str,
    timeout: int = 30,
) -> None:
    """确保远端节点已安装 tmux，缺失时尝试自动安装。

    ssh_argv 是完整的 ssh 命令（不含远程命令部分），如：
    ["ssh", "-p", "22", "-i", "...", "user@host"]
    ssh_env 是 ssh 环境变量。
    """
    script = build_remote_tmux_install_script()
    result = subprocess.run(
        [*ssh_argv, script],
        capture_output=True,
        text=True,
        env=ssh_env,
        timeout=timeout,
    )
    if result.returncode == 0 and result.stdout.strip():
        return
    if result.returncode != 0:
        # ssh 本身失败或远端命令返回非零
        tmux_path = result.stdout.strip()
        if tmux_path:
            return
        raise ValueError(
            f"节点 {node_name} 未安装 tmux 且无法自动安装"
            f"（无 passwordless sudo 或无支持的包管理器），"
            f"请 SSH 登录该节点手动安装：apt install tmux / dnf install tmux"
        )


def tmux_socket_name(state_dir: Path) -> str:
    """根据 state_dir 生成 tmux socket 名（用于 -L 隔离多实例）。"""
    digest = hashlib.sha256(str(state_dir).encode()).hexdigest()[:8]
    return f"expsched_{digest}"


def tmux_base_command(socket: str) -> list[str]:
    """构造 tmux 基础命令前缀（含 -L socket -u）。"""
    return ["tmux", "-L", socket, "-u"]


def build_tmux_new_command(
    socket: str,
    *,
    session_name: str,
    cols: int,
    rows: int,
    history_limit: int,
    log_dir: Path,
    shell: str = "",
) -> list[str]:
    """构造本地 tmux 创建命令（detached session）。

    使用 bash -lc 启动登录 shell（保证 conda init 生效）。
    history-limit 与 pipe-pane 在 session 创建后由 build_tmux_setup_command 配置。
    """
    tmux = tmux_base_command(socket)
    log_dir.mkdir(parents=True, exist_ok=True)

    shell_cmd = shell or os.environ.get("SHELL") or "/bin/bash"
    return [
        *tmux,
        "new-session", "-d", "-s", session_name,
        "-x", str(cols), "-y", str(rows),
        f"env TERM=xterm-256color bash -lc 'exec {shell_cmd} -l'",
    ]


def build_tmux_setup_command(
    socket: str,
    *,
    session_name: str,
    history_limit: int,
    log_path: Path,
) -> list[str]:
    """构造 tmux session 配置命令（history-limit + pipe-pane）。

    在 new-session 之后单独执行，因为 history-limit 需要在 session 创建后设置。
    """
    tmux = tmux_base_command(socket)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(mode=0o600, exist_ok=True)
    pipe_cmd = f"cat >> {shlex.quote(str(log_path))}"
    return [
        *tmux,
        "set-option", "-t", session_name, "history-limit", str(history_limit), ";",
        "set-option", "-t", session_name, "status", "off", ";",
        "pipe-pane", "-t", session_name, "-o", pipe_cmd,
    ]


def build_tmux_attach_command(
    socket: str,
    *,
    session_name: str,
) -> list[str]:
    """构造 tmux attach 命令（用于 PTY 子进程）。"""
    tmux = tmux_base_command(socket)
    return [*tmux, "attach-session", "-t", session_name]


def build_tmux_kill_command(
    socket: str,
    *,
    session_name: str,
) -> list[str]:
    """构造 tmux kill-session 命令。"""
    tmux = tmux_base_command(socket)
    return [*tmux, "kill-session", "-t", session_name]


def build_tmux_has_session_command(
    socket: str,
    *,
    session_name: str,
) -> list[str]:
    """构造 tmux has-session 检测命令。"""
    tmux = tmux_base_command(socket)
    return [*tmux, "has-session", "-t", session_name]


def build_tmux_capture_pane_command(
    socket: str,
    *,
    session_name: str,
) -> list[str]:
    """构造 tmux capture-pane 命令（全 history，纯文本）。

    -p: 输出到 stdout
    -S -: 从 history 起始行开始
    """
    tmux = tmux_base_command(socket)
    return [*tmux, "capture-pane", "-p", "-S", "-", "-t", session_name]


def build_tmux_resize_command(
    socket: str,
    *,
    session_name: str,
    cols: int,
    rows: int,
) -> list[str]:
    """构造 tmux resize-window 命令。"""
    tmux = tmux_base_command(socket)
    return [*tmux, "resize-window", "-t", session_name, "-x", str(cols), "-y", str(rows)]


def build_tmux_list_sessions_command(socket: str) -> list[str]:
    """构造 tmux list-sessions 命令。"""
    tmux = tmux_base_command(socket)
    return [*tmux, "list-sessions", "-F", "#{session_name}"]


def build_tmux_load_buffer_command(socket: str, *, buffer_name: str) -> list[str]:
    """构造 tmux load-buffer 命令，从 stdin 读取内容。"""
    tmux = tmux_base_command(socket)
    return [*tmux, "load-buffer", "-b", buffer_name, "-"]


def build_tmux_paste_buffer_command(
    socket: str,
    *,
    session_name: str,
    buffer_name: str,
) -> list[str]:
    """构造 tmux paste-buffer 命令，将 buffer 注入目标 pane。"""
    tmux = tmux_base_command(socket)
    return [
        *tmux,
        "paste-buffer",
        "-d",
        "-b",
        buffer_name,
        "-t",
        session_name,
    ]


def build_tmux_send_enter_command(socket: str, *, session_name: str) -> list[str]:
    """构造 tmux send-keys Enter 命令。"""
    tmux = tmux_base_command(socket)
    return [*tmux, "send-keys", "-t", session_name, "Enter"]


def tmux_has_session(socket: str, session_name: str) -> bool:
    """检测 tmux session 是否存在。"""
    cmd = build_tmux_has_session_command(socket, session_name=session_name)
    result = subprocess.run(cmd, capture_output=True, timeout=5)
    return result.returncode == 0


def tmux_kill_session(socket: str, session_name: str) -> None:
    """杀死 tmux session（pipe-pane 自动停止）。"""
    cmd = build_tmux_kill_command(socket, session_name=session_name)
    subprocess.run(cmd, capture_output=True, timeout=10)


def tmux_capture_pane(socket: str, session_name: str) -> bytes:
    """捕获 tmux session 的完整 history（含 ANSI 颜色）。"""
    cmd = build_tmux_capture_pane_command(socket, session_name=session_name)
    result = subprocess.run(cmd, capture_output=True, timeout=10)
    if result.returncode != 0:
        return b""
    return result.stdout


def tmux_disable_status(socket: str, session_name: str) -> None:
    """关闭 tmux 状态栏，避免状态栏进入浏览器 xterm 显示/滚动历史。"""
    tmux = tmux_base_command(socket)
    subprocess.run(
        [*tmux, "set-option", "-t", session_name, "status", "off"],
        capture_output=True,
        timeout=5,
    )


def tmux_resize_window(
    socket: str, session_name: str, *, cols: int, rows: int
) -> None:
    """调整 tmux window 尺寸。"""
    cmd = build_tmux_resize_command(
        socket, session_name=session_name, cols=cols, rows=rows
    )
    subprocess.run(cmd, capture_output=True, timeout=5)


def tmux_list_sessions(socket: str) -> list[str]:
    """列出当前 socket 下所有 tmux session 名。"""
    cmd = build_tmux_list_sessions_command(socket)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def tmux_paste_input(
    socket: str,
    session_name: str,
    data: bytes,
    *,
    timeout: int = 10,
) -> None:
    """通过 tmux buffer 向目标 pane 注入输入。"""
    buffer_name = f"exp-scheduler-{os.getpid()}-{time.monotonic_ns()}"
    load_cmd = build_tmux_load_buffer_command(socket, buffer_name=buffer_name)
    load = subprocess.run(
        load_cmd,
        input=data,
        capture_output=True,
        timeout=timeout,
    )
    if load.returncode != 0:
        stderr = load.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"tmux 写入启动命令失败: {stderr or '未知错误'}")

    paste_cmd = build_tmux_paste_buffer_command(
        socket,
        session_name=session_name,
        buffer_name=buffer_name,
    )
    paste = subprocess.run(paste_cmd, capture_output=True, timeout=timeout)
    if paste.returncode != 0:
        stderr = paste.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"tmux 注入启动命令失败: {stderr or '未知错误'}")

    enter_cmd = build_tmux_send_enter_command(socket, session_name=session_name)
    enter = subprocess.run(enter_cmd, capture_output=True, timeout=timeout)
    if enter.returncode != 0:
        stderr = enter.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"tmux 提交启动命令失败: {stderr or '未知错误'}")


# ---------- 远端 tmux 命令构造 ----------

def build_remote_tmux_new_script(
    *,
    session_name: str,
    cols: int,
    rows: int,
    history_limit: int,
    remote_log_dir: str,
) -> str:
    """构造远端 tmux 创建+配置脚本（用于 ssh 命令）。

    在远端创建 detached session、设置 history-limit、启动 pipe-pane。
    pipe-pane 日志写入远端 remote_log_dir/<session_name>.stream.log。
    """
    remote_log_dir_arg = _remote_expandable_path(remote_log_dir)
    remote_log_path = f"{remote_log_dir}/{session_name}.stream.log"
    remote_log_path_arg = _remote_expandable_path(remote_log_path)
    pipe_cmd = shlex.quote(f"cat >> {remote_log_path_arg}")
    return (
        f"mkdir -p {remote_log_dir_arg} && "
        f"touch {remote_log_path_arg} && "
        f"chmod 600 {remote_log_path_arg} && "
        f"tmux -u new-session -d -s {session_name} -x {cols} -y {rows} "
        f"'env TERM=xterm-256color bash -lc \"exec \\$SHELL -l\"' && "
        f"tmux -u set-option -t {session_name} history-limit {history_limit} && "
        f"tmux -u set-option -t {session_name} status off && "
        f"tmux -u pipe-pane -t {session_name} -o {pipe_cmd}"
    )


def _remote_expandable_path(path: str) -> str:
    if path.startswith("$HOME/"):
        return '"$HOME/' + path[len("$HOME/") :].replace('"', '\\"') + '"'
    return shlex.quote(path)


def build_remote_tmux_attach_argv(
    ssh_prefix: list[str],
    *,
    session_name: str,
) -> list[str]:
    """构造远端 tmux attach 命令（用于 PTY 子进程）。

    ssh_prefix 是完整的 ssh 前缀（含 -tt、端口、密钥等），不含远程命令。
    """
    remote_cmd = f"tmux -u attach-session -t {session_name}"
    return [*ssh_prefix, remote_cmd]


def build_remote_tmux_kill_argv(
    ssh_prefix: list[str],
    *,
    session_name: str,
) -> list[str]:
    """构造远端 tmux kill-session 命令。"""
    remote_cmd = f"tmux -u kill-session -t {session_name}"
    return [*ssh_prefix, remote_cmd]


def build_remote_tmux_has_argv(
    ssh_prefix: list[str],
    *,
    session_name: str,
) -> list[str]:
    """构造远端 tmux has-session 检测命令。"""
    remote_cmd = f"tmux -u has-session -t {session_name}"
    return [*ssh_prefix, remote_cmd]


def build_remote_tmux_capture_argv(
    ssh_prefix: list[str],
    *,
    session_name: str,
) -> list[str]:
    """构造远端 tmux capture-pane 命令。"""
    remote_cmd = f"tmux -u capture-pane -p -S - -t {session_name}"
    return [*ssh_prefix, remote_cmd]


def build_remote_tmux_resize_argv(
    ssh_prefix: list[str],
    *,
    session_name: str,
    cols: int,
    rows: int,
) -> list[str]:
    """构造远端 tmux resize-window 命令。"""
    remote_cmd = f"tmux -u resize-window -t {session_name} -x {cols} -y {rows}"
    return [*ssh_prefix, remote_cmd]


def build_remote_tmux_paste_argv(
    ssh_prefix: list[str],
    *,
    session_name: str,
    buffer_name: str,
) -> list[str]:
    """构造远端 tmux buffer 注入命令。"""
    quoted_buffer = shlex.quote(buffer_name)
    quoted_session = shlex.quote(session_name)
    remote_cmd = (
        f"tmux -u load-buffer -b {quoted_buffer} - && "
        f"tmux -u paste-buffer -d -b {quoted_buffer} -t {quoted_session} && "
        f"tmux -u send-keys -t {quoted_session} Enter"
    )
    return [*ssh_prefix, remote_cmd]


def remote_tmux_has_session(
    ssh_prefix: list[str],
    ssh_env: dict[str, str],
    *,
    session_name: str,
    timeout: int = 10,
) -> bool:
    """检测远端 tmux session 是否存在。"""
    cmd = build_remote_tmux_has_argv(ssh_prefix, session_name=session_name)
    result = subprocess.run(
        cmd, capture_output=True, env=ssh_env, timeout=timeout
    )
    return result.returncode == 0


def remote_tmux_kill_session(
    ssh_prefix: list[str],
    ssh_env: dict[str, str],
    *,
    session_name: str,
    timeout: int = 10,
) -> None:
    """杀死远端 tmux session。"""
    cmd = build_remote_tmux_kill_argv(ssh_prefix, session_name=session_name)
    subprocess.run(cmd, capture_output=True, env=ssh_env, timeout=timeout)


def remote_tmux_capture_pane(
    ssh_prefix: list[str],
    ssh_env: dict[str, str],
    *,
    session_name: str,
    timeout: int = 15,
) -> bytes:
    """捕获远端 tmux session 的完整 history。"""
    cmd = build_remote_tmux_capture_argv(ssh_prefix, session_name=session_name)
    result = subprocess.run(
        cmd, capture_output=True, env=ssh_env, timeout=timeout
    )
    if result.returncode != 0:
        return b""
    return result.stdout


def remote_tmux_disable_status(
    ssh_prefix: list[str],
    ssh_env: dict[str, str],
    *,
    session_name: str,
    timeout: int = 10,
) -> None:
    """关闭远端 tmux 状态栏。"""
    subprocess.run(
        [*ssh_prefix, f"tmux -u set-option -t {session_name} status off"],
        capture_output=True,
        env=ssh_env,
        timeout=timeout,
    )


def remote_tmux_resize(
    ssh_prefix: list[str],
    ssh_env: dict[str, str],
    *,
    session_name: str,
    cols: int,
    rows: int,
    timeout: int = 10,
) -> None:
    """调整远端 tmux window 尺寸。"""
    cmd = build_remote_tmux_resize_argv(
        ssh_prefix, session_name=session_name, cols=cols, rows=rows
    )
    subprocess.run(cmd, capture_output=True, env=ssh_env, timeout=timeout)


def remote_tmux_paste_input(
    ssh_prefix: list[str],
    ssh_env: dict[str, str],
    *,
    session_name: str,
    data: bytes,
    timeout: int = 15,
) -> None:
    """通过远端 tmux buffer 向目标 pane 注入输入。"""
    buffer_name = f"exp-scheduler-{os.getpid()}-{time.monotonic_ns()}"
    cmd = build_remote_tmux_paste_argv(
        ssh_prefix,
        session_name=session_name,
        buffer_name=buffer_name,
    )
    result = subprocess.run(
        cmd,
        input=data,
        capture_output=True,
        env=ssh_env,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"远端 tmux 注入启动命令失败: {stderr or '未知错误'}")
