# exp-scheduler

GPU 实验任务调度器。它会持续观察服务器上的 GPU 空闲情况，把你提交的命令按队列顺序自动运行，并提供一个适合通过 SSH 隧道访问的 Web 界面。

## 特性
- 单任务独占单卡调度
- 支持每个任务单独指定 GPU，默认自动分配
- 支持设置全局可调度 GPU 白名单，默认全部可用且可在页面实时修改
- 支持关闭 GPU 后按连续空闲时长自动恢复可用，默认 5 分钟，也可关闭
- 支持连续空闲检测与网页调控器，避免 GPU 刚释放时立即撞车
- 支持任务被外部信号杀掉后让对应 GPU 进入可配置冷却时间
- 支持紧急任务队列，以及把运行中的任务抢占回普通队列队首
- SQLite 持久化队列和历史
- FastAPI Web 服务 + React/Vite 前端
- 支持新增、删除、重排、取消、重新入队、暂停/恢复调度
- 支持在界面头部显示当前服务器名称和 IP，方便多机区分
- 支持按 `wait_and_run.sh` 风格对 OOM / 资源类错误自动重试，且由全局配置统一控制
- 每个任务独立日志，运行中任务以只读终端实时查看，历史任务保留纯文本日志视图

## 安装依赖

假设仓库位于 `/data/ykw/my-tools`。如果放在其他目录，下面命令里的路径需要对应替换。

```bash
cd /data/ykw/my-tools
python3 -m venv .venv
source .venv/bin/activate
pip install -r tools/exp-scheduler/requirements-dev.txt
```

## 初始化

```bash
cd /data/ykw/my-tools
./scripts/exp_scheduler.py init
./scripts/exp_scheduler.py doctor
```

默认配置文件位于：

```text
~/.config/exp-scheduler/config.toml
```

建议保持默认的：

```toml
host = "127.0.0.1"
port = 17861
```

这样服务只监听服务器本机，远程访问通过 SSH 隧道转发，不直接暴露到局域网。

常用配置项：

```toml
poll_interval_seconds = 5
gpu_idle_memory_mb = 2000
gpu_idle_required_checks = 6
auto_restore_idle_gpu_seconds = 300
auto_retry_max_retries = 0
auto_retry_delay_seconds = 5
external_kill_gpu_cooldown_seconds = 300
state_dir = "/home/ykw/.local/share/exp-scheduler"
log_dir = "/home/ykw/.local/share/exp-scheduler/logs"
```

这些配置的含义：

- `poll_interval_seconds`：调度循环间隔，单位是秒。每隔这个时间检查一次 GPU 状态、队列和定时开关 GPU 的计划。数值越小响应越快，但查询 GPU 更频繁。
- `gpu_idle_memory_mb`：默认空闲显存阈值，单位是 MB。任务没有单独填写“进程预算”时，GPU 已用显存低于这个值才会被认为可调度。
- `gpu_idle_required_checks`：GPU 连续多少次满足可启动条件后才启动任务，默认 `6`。配合默认 `poll_interval_seconds = 5` 时，外部释放或未知占用状态下约等待 30 秒稳定窗口。调度器自己管理的任务结束后，只要下一次探测确认对应 GPU 可用，就会快速接续下一个任务。这个值也可以在网页“调控器”页实时修改并写入 SQLite 状态库。
- `auto_restore_idle_gpu_seconds`：被全局关闭的 GPU 连续空闲多久后自动恢复可用，单位是秒，默认 `300`。设为 `0` 可关闭默认自动恢复；也可以在网页 GPU 资源池里实时关闭并写入 SQLite 状态库。
- `auto_retry_max_retries`：自动重试次数。为 `0` 时不自动重试；大于 `0` 时，遇到 OOM / 资源类错误会重新排队重试。也可以在网页“资源与环境”页实时修改并写入 SQLite 状态库。
- `auto_retry_delay_seconds`：自动重试前等待的秒数，只在 `auto_retry_max_retries > 0` 时生效，也可以在网页里实时修改。
- `external_kill_gpu_cooldown_seconds`：任务被外部 `kill`、系统 OOM killer 或其他外部信号中断后，对应 GPU 暂停接收新任务多久，单位是秒，默认 `300`。设为 `0` 可关闭；也可以在网页“调控器”页实时修改。
- `state_dir`：状态目录，默认保存 `scheduler.db`。队列、历史记录、环境 profile、GPU 开关配置、定时计划等持久化状态都在这个 SQLite 数据库里。
- `log_dir`：任务日志目录。新任务启动时会把日志写到这里，并把日志文件的绝对路径保存到 `scheduler.db` 的任务记录中。

## 迁移状态和日志目录

不要在任务运行中迁移目录。停止调度器会中断正在运行的任务，所以建议先在页面里暂停调度，等待运行中的任务结束，再继续下面步骤。SQLite 可能会产生 `scheduler.db-wal`、`scheduler.db-shm` 这类附属文件，因此要在服务停止后复制整个状态目录。

如果要把：

```toml
state_dir = "/home/ykw/.local/share/exp-scheduler"
log_dir = "/home/ykw/.local/share/exp-scheduler/logs"
```

迁移到例如：

```toml
state_dir = "/data/ykw/exp-scheduler-state"
log_dir = "/data/ykw/exp-scheduler-state/logs"
```

按下面步骤迁移。

1. 设置本次迁移用到的路径变量。

```bash
cd /data/ykw/my-tools

OLD_STATE=/home/ykw/.local/share/exp-scheduler
OLD_LOG=/home/ykw/.local/share/exp-scheduler/logs
NEW_STATE=/data/ykw/exp-scheduler-state
NEW_LOG=/data/ykw/exp-scheduler-state/logs
```

2. 确认没有运行中的任务。

最稳的是先在页面里暂停调度，等“运行中”列表为空。也可以用接口看一下：

```bash
curl -s http://127.0.0.1:17861/api/tasks > /tmp/exp-scheduler-tasks.json

.venv/bin/python - <<'PY'
import json
from pathlib import Path

data = json.loads(Path("/tmp/exp-scheduler-tasks.json").read_text())
running = data.get("running", [])
if running:
    for task in running:
        print(f"running: id={task['id']} name={task['name']} pid={task.get('pid')}")
else:
    print("no running tasks")
PY
```

3. 停止调度器。

如果已经用 systemd 管理：

```bash
systemctl --user stop exp-scheduler
```

如果还没有交给 systemd 管理，而是用 `./scripts/exp_scheduler.py serve` 前台启动，先在对应终端按 `Ctrl-C` 停止。确认端口已经没有监听：

```bash
ss -ltnp '( sport = :17861 )'
```

如果还有 `LISTEN`，先确认它是不是旧的调度器进程：

```bash
pgrep -af 'scripts/exp_scheduler.py serve'
```

4. 复制旧状态和旧日志。

```bash
mkdir -p "$NEW_STATE" "$NEW_LOG"
rsync -a "$OLD_STATE"/ "$NEW_STATE"/
rsync -a "$OLD_LOG"/ "$NEW_LOG"/
```

如果旧的 `log_dir` 本来就在 `state_dir/logs` 下面，第一条 `rsync` 已经会把日志一起复制过去；第二条 `rsync` 仍然可以保留，用来确保日志目录完整。

5. 备份并修改配置文件。

```bash
cp -a ~/.config/exp-scheduler/config.toml ~/.config/exp-scheduler/config.toml.bak.$(date +%Y%m%d-%H%M%S)
```

然后编辑 `~/.config/exp-scheduler/config.toml`，改成新目录：

```toml
state_dir = "/data/ykw/exp-scheduler-state"
log_dir = "/data/ykw/exp-scheduler-state/logs"
```

配置文件本身仍然放在 `~/.config/exp-scheduler/config.toml`，不用移动。

6. 更新数据库里的历史日志路径。

由于历史任务的 `log_path` 是绝对路径，日志目录移动后必须更新 `scheduler.db` 里的旧路径，否则历史记录里点日志会找旧位置。

```bash
cd /data/ykw/my-tools

OLD_LOG=/home/ykw/.local/share/exp-scheduler/logs
NEW_LOG=/data/ykw/exp-scheduler-state/logs
NEW_STATE=/data/ykw/exp-scheduler-state

DB_BACKUP="$NEW_STATE/migration-db-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DB_BACKUP"
cp -a "$NEW_STATE"/scheduler.db* "$DB_BACKUP"/ 2>/dev/null || true

OLD_LOG="$OLD_LOG" NEW_LOG="$NEW_LOG" NEW_STATE="$NEW_STATE" .venv/bin/python - <<'PY'
import os
import sqlite3
from pathlib import Path

db_path = Path(os.environ["NEW_STATE"]) / "scheduler.db"
old_prefix = os.environ["OLD_LOG"].rstrip("/") + "/"
new_prefix = os.environ["NEW_LOG"].rstrip("/") + "/"

with sqlite3.connect(db_path) as conn:
    conn.execute(
        "UPDATE tasks SET log_path = replace(log_path, ?, ?) WHERE log_path LIKE ?",
        (old_prefix, new_prefix, old_prefix + "%"),
    )
    conn.commit()
PY
```

7. 启动并检查。

```bash
systemctl --user start exp-scheduler
./scripts/exp_scheduler.py doctor
curl http://127.0.0.1:17861/api/server
```

再打开页面检查三件事：

- 队列和历史记录还在。
- 历史记录里的日志能打开。
- 新提交一个小任务后，日志写入新 `log_dir`。

可以用下面命令确认数据库里没有旧日志路径：

```bash
NEW_STATE=/data/ykw/exp-scheduler-state
OLD_LOG=/home/ykw/.local/share/exp-scheduler/logs

OLD_LOG="$OLD_LOG" NEW_STATE="$NEW_STATE" .venv/bin/python - <<'PY'
import os
import sqlite3
from pathlib import Path

db_path = Path(os.environ["NEW_STATE"]) / "scheduler.db"
old_prefix = os.environ["OLD_LOG"].rstrip("/") + "/"

with sqlite3.connect(db_path) as conn:
    count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE log_path LIKE ?",
        (old_prefix + "%",),
    ).fetchone()[0]
print(f"old log_path rows: {count}")
PY
```

确认队列、历史记录和历史日志都正常后，再按需要删除旧目录。建议先保留旧目录一段时间，确认没有问题后再清理。

如果启动后发现不对，可以先停止服务，把 `config.toml` 改回旧路径，再启动服务。旧目录还保留时，这个回滚最快。

## 启动服务

前台启动，适合临时测试：

```bash
cd /data/ykw/my-tools
./scripts/exp_scheduler.py serve
```

确认页面和接口可用：

```bash
curl http://127.0.0.1:17861/api/server
```

## 配置为常驻服务

推荐使用用户级 systemd。这样不用每次手动进目录启动，服务异常退出后也会自动重启。

创建服务文件：

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/exp-scheduler.service <<'EOF'
[Unit]
Description=exp-scheduler GPU task scheduler
After=network.target

[Service]
Type=simple
WorkingDirectory=/data/ykw/my-tools
ExecStart=/data/ykw/my-tools/.venv/bin/python /data/ykw/my-tools/scripts/exp_scheduler.py serve
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment="PATH=/data/ykw/my-tools/.venv/bin:/home/ykw/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"

[Install]
WantedBy=default.target
EOF
```

如果仓库路径不是 `/data/ykw/my-tools`，需要修改 `WorkingDirectory`、`ExecStart` 和 `PATH` 里的 `.venv/bin` 路径。如果 `nvitop` 安装在其他 conda 路径，也需要把对应的 `bin` 目录加入 `PATH`，否则 GPU 监控页会提示找不到 `nvitop`。

启用并启动：

```bash
systemctl --user daemon-reload
systemctl --user enable --now exp-scheduler
systemctl --user status exp-scheduler
```

`exp-scheduler` 和 `exp-scheduler.service` 是同一个服务，下面两种写法等价：

```bash
systemctl --user start exp-scheduler
systemctl --user start exp-scheduler.service
```

常用管理命令：

```bash
systemctl --user restart exp-scheduler
systemctl --user stop exp-scheduler
systemctl --user status exp-scheduler
journalctl --user -u exp-scheduler -f
```

如果希望服务器重启后、用户还没有登录时也能自动启动，需要管理员权限执行一次：

```bash
sudo loginctl enable-linger ykw
```

如果 `systemctl --user start exp-scheduler` 提示端口已占用，通常是因为已经用 `./scripts/exp_scheduler.py serve` 手动启动过旧进程。先确认进程：

```bash
ss -ltnp '( sport = :17861 )'
```

确认没有任务正在运行后，再停止旧进程并交给 systemd 管理。

## 前端源码

前端源码位于 `tools/exp-scheduler/frontend`，构建产物会发布到 `tools/exp-scheduler/src/exp_scheduler_app/static`：

第一次构建或依赖变化时先安装依赖：

```bash
cd tools/exp-scheduler/frontend
npm install
```

修改前端源码后，重新构建并同步到后端静态目录：

```bash
cd tools/exp-scheduler/frontend
npm run lint
npm run build
rm -rf ../src/exp_scheduler_app/static/assets
cp dist/index.html ../src/exp_scheduler_app/static/index.html
cp -R dist/assets ../src/exp_scheduler_app/static/assets
```

如果服务已经在运行，构建同步后刷新浏览器即可加载新前端；如果同时修改了后端代码，则需要重启服务。

## 浏览器访问

推荐通过 SSH 本地端口转发访问。服务端配置保持 `host = "127.0.0.1"`，在自己的电脑上执行：

```bash
ssh -L 17861:127.0.0.1:17861 <server>
```

然后打开：

```text
http://127.0.0.1:17861
```

也可以写入本机的 `~/.ssh/config`：

```sshconfig
Host exp-server
  HostName 222.20.98.71
  User ykw
  ServerAliveInterval 60
  LocalForward 17861 127.0.0.1:17861
```

之后执行：

```bash
ssh exp-server
```

隧道保持连接期间，在本机浏览器打开 `http://127.0.0.1:17861` 即可访问调度器。

如果要同时连接多台服务器，本机端口需要错开，例如：

```sshconfig
Host exp-server-a
  HostName 222.20.98.71
  User ykw
  LocalForward 17861 127.0.0.1:17861

Host exp-server-b
  HostName 222.20.98.72
  User ykw
  LocalForward 17862 127.0.0.1:17861
```

## 日常使用

服务启动后，在浏览器里打开调度器页面：

```text
http://127.0.0.1:17861
```

新建任务时常用字段：

- `名称`：任务在队列和历史记录里的显示名。
- `工作目录`：命令执行所在目录，例如 `/data/ykw/projects/ragkv`。
- `命令`：实际运行的 shell 命令，和你在终端中执行的命令保持一致。
- `运行环境`：选择已经导入的 conda/venv profile，或填写任务自己的 shell setup。
- `指定 GPU`：可选；不填时由调度器自动选择可用 GPU。
- `进程预算`：可选；不同任务显存需求不同时填写。若当前显存余量连续多次大于预算加 2GB，任务可以启动；不填则使用默认空闲阈值。

任务队列页可以新增、编辑、重排、取消、重新入队和批量删除任务。队列中的删除表示取消任务；历史记录中的删除表示删除历史记录。

点击“暂停调度”时，如果当前还有运行中的任务，页面会询问是否同时停止这些任务并放回原队列队首。选择停止后，调度器会先暂停新调度，再向运行中的任务发送停止信号；任务回到队首后会等待恢复调度。

在资源与环境页关闭某个 GPU 的调度时，如果该 GPU 上有正在运行的任务，页面会询问是否停止这些任务并放回原队列队首。选择不停止时，该 GPU 不再接收后续新任务，但当前已经运行的任务会继续执行。

调度器停止运行中任务时会先发送 `SIGINT`，效果接近终端里按 `Ctrl-C`；如果任务没有退出，5 秒后发送 `SIGTERM`，再过 5 秒仍未退出才用 `SIGKILL` 兜底。

如果调度服务停止，或任务进程被外部 `kill` 信号中断，调度器会把该任务放回原队列队首，下一次调度时自动重试。外部信号中断还会让对应 GPU 进入 `external_kill_gpu_cooldown_seconds` 冷却时间，避免调度器立刻重新占回别人刚释放的卡。页面里的“取消任务”表示用户主动取消，不会自动重试，也不会触发这个外部 kill 冷却。

运行中的任务日志会以只读终端实时显示，也可以在日志面板里切回之前的 attempt 日志。历史记录里的日志是任务结束后保存的纯文本日志；双击历史记录可以打开日志，再次双击关闭。单个 attempt 日志可以单独删除，删除前页面会确认一次，任务记录本身会保留。

GPU 页面可以控制哪些 GPU 参与调度，也可以给单张 GPU 设置定时开启或关闭。“调控器”页面可以实时调整检测间隔、连续满足次数，以及 OOM / CUDA 资源类错误自动重试策略。监控页面会在内嵌终端中运行 `nvitop`。

## 日志查看

运行中任务会以只读 PTY 终端形式展示，因此颜色、进度条和覆盖刷新类输出都能正常显示。任务结束后，页面会自动切回历史纯文本日志视图；浏览器侧在 v1 不支持任意键盘输入，只提供查看能力。

多次运行或自动重试的任务会保留多个日志文件，页面可以按 attempt 切换查看。删除日志只会删除选中的 attempt 日志文件，不会删除任务记录；正在运行并仍在写入的当前日志不允许删除。
