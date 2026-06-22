# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# exp-scheduler

GPU 实验任务调度器 — 在多 GPU 服务器上排队、调度和管理深度学习训练等实验任务。FastAPI 后端 + React/Vite 前端，SQLite 持久化，无第三方 ORM。

## 项目结构

```
pyproject.toml                    # Python 包配置与 exp-scheduler 命令入口
src/exp_scheduler_app/            # Python 后端包
  cli.py                          # argparse: init, serve, doctor
  config.py                       # TOML 配置加载 (dataclass SchedulerConfig)
  database.py                     # SQLite ORM (threading.RLock, WAL mode)
  scheduler.py                    # 核心调度引擎 (SchedulerService)
  web.py                          # FastAPI 应用 + API 路由 + SSE
  gpu.py                          # nvidia-smi GPU 查询
  events.py                       # 异步事件发布/订阅 (asyncio.Queue)
  terminal.py                     # PTY 终端会话管理
  system_terminal.py              # nvitop 系统终端服务
  profile_discovery.py            # conda/venv 环境自动发现
  nodes.py                        # 节点注册表 + SSH 密钥库 (NodeRegistryService)
  transfer.py                     # rsync 文件同步任务 (TransferService)
  interactive_terminal.py         # 多节点交互终端 (InteractiveTerminalService)
  conda_inventory.py              # 各节点 conda 环境对比 (CondaInventoryService)
  static/                         # 前端构建产物，由 FastAPI 静态服务（需手动同步，见下）
frontend/                         # React 前端源码
  src/App.tsx                     # 单文件 React 应用 (~4000 行)
  src/index.css                   # Tailwind + 自定义样式
tests/                            # pytest 测试
skills/exp-scheduler-gpu-lease/   # Codex/agent 控制 skill（HTTP API 包装脚本）
deploy/exp-scheduler.service      # systemd 用户服务模板
```

## 常用命令

```bash
# 安装（开发模式，含 pytest/httpx）
pip install -e ".[dev]"

# 后端
exp-scheduler init                 # 初始化配置和数据库
exp-scheduler serve                # 启动 Web 服务 (默认 127.0.0.1:17861)
exp-scheduler doctor               # 环境诊断

# 测试
pytest tests/ -v                            # 全部测试
pytest tests/test_scheduler.py -v           # 单个文件
pytest tests/test_api.py -v -k test_name    # 单个测试
pytest tests/test_nodes.py -v               # 节点注册表/SSH 密钥库测试
pytest tests/test_transfers.py -v           # 文件同步测试
pytest tests/test_interactive_terminal.py -v # 交互终端测试

# 前端（修改 App.tsx 后必须构建并手动同步到后端静态目录）
cd frontend
npm run lint                   # TypeScript 类型检查 (tsc --noEmit)
npm run build                  # Vite 构建到 frontend/dist/ (--base=/static/)
rm -rf ../src/exp_scheduler_app/static/assets
cp dist/index.html ../src/exp_scheduler_app/static/index.html
cp -R dist/assets ../src/exp_scheduler_app/static/assets
```

前端构建产物不会自动进入 `static/`，必须执行上面的复制步骤。后端服务运行中时，同步后刷新浏览器即可；改了后端代码则需重启服务。

## 架构要点

- **调度模型**：单 GPU 单任务。GPU 需通过 N 次连续空闲检测（默认 6×5s=30s）才分配任务；任务可单独指定 GPU 或"进程预算"（显存 MB），否则用全局空闲阈值
- **三条队列**：`normal` / `urgent` / `staged`（暂存，不参与调度）。`urgent` 可抢占 `normal` 任务，抢占使用 SIGINT→5s→SIGTERM→5s→SIGKILL 梯度
- **中断恢复**：服务重启或外部信号杀死的任务自动回到队首；外部 kill 还会让对应 GPU 进入可配置冷却时间（默认 300s）
- **OOM 重试**：检测 CUDA OOM / exit code 137/143，可配置重试次数和延迟；多次 attempt 各保留独立日志（`task_attempts` 表）
- **任务依赖 (DAG)**：`task_dependencies` 表存储依赖边，调度器通过 `are_dependencies_satisfied` 检查所有依赖 `succeeded` 后才调度；递归 CTE 检测循环；删除任务时手动清理依赖边
- **Agent GPU lease**：`/api/agent/gpu-leases` 让外部 agent 临时占用指定 GPU（`agent_gpu_leases` 表，支持 TTL 和 `stop_running`）。有效可调度 GPU = 用户白名单 − 活跃 lease；lease 不改写用户白名单，空闲自动恢复不会抢回被 lease 的 GPU。`skills/exp-scheduler-gpu-lease/` 是对应的 agent 包装脚本（lease + task-create/list/reorder 等命令）
- **文件同步**：节点注册表（`nodes`/`ssh_keys` 表）+ 有向连通性矩阵（`node_links` 表），传输路由自动选择——direct（一端直连另一端）优先，bridged（经主控 `ssh -R` 隧道桥接）兜底；rsync 子进程走 PIPE（非 PTY），逐行解析 `--info=progress2` 输出结构化进度（百分比/速率/ETA）
- **交互终端**：多节点 PTY 交互终端，输入走 `POST /api/terminals/{sid}/input`、输出走 SSE（不用 WebSocket）；广播模式在前端 fan-out（onData 复制到所有勾选会话），后端不感知广播
- **实时通信**：SSE 推送任务状态变更和终端输出，前端用 xterm.js 渲染 PTY 流
- **配置**：TOML 文件 (`~/.config/exp-scheduler/config.toml`)，状态库默认在 `~/.local/share/exp-scheduler/scheduler.db`；部分设置（检测次数、重试策略、GPU 开关等）可通过 Web UI 运行时修改并持久化到 SQLite meta 表

## 编码规范

- Python: `from __future__ import annotations` 在每个文件开头；使用 `dataclass(slots=True)`；类型注解使用 `str | None` 而非 `Optional[str]`
- 后端无 ORM 库，直接用 `sqlite3` + 手写 SQL，`Database` 类用 `threading.RLock` 保证线程安全
- API 请求/响应用 Pydantic `BaseModel` 定义在 `web.py` 顶部
- 测试中用 `FakeGPUProvider` mock GPU 查询，用 `TestClient` 测试 API
- 前端是单文件 React 应用 (App.tsx)，使用 Tailwind CSS 4 + Motion 动画，API 调用走相对路径 `fetch`
- 中文注释和 UI 文本，英文代码标识符

## 添加新 API 端点的模式

1. 在 `web.py` 顶部定义 Pydantic 请求模型
2. 在 `create_app()` 内部的闭包函数中添加路由（依赖 `scheduler`、`db`、`event_broker` 等闭包变量）
3. 操作完成后通过 `event_broker.publish()` 发送 SSE 事件
4. 在 `tests/test_api.py` 中用 `TestClient` 测试

## 添加新数据库字段

1. 在 `database.py` 的 `_ensure_columns()` 方法中添加 `ALTER TABLE ... ADD COLUMN` 语句（自动迁移）
2. 更新对应的 `CREATE TABLE` 语句和查询方法
