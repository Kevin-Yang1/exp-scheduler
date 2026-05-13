# exp-scheduler

GPU 实验任务调度器 — 在多 GPU 服务器上排队、调度和管理深度学习训练等实验任务。

## 项目结构

```
scripts/exp_scheduler.py          # CLI 入口，调用 src/exp_scheduler_app/cli.py
tools/exp-scheduler/
  src/exp_scheduler_app/          # Python 后端包
    cli.py                        # argparse: init, serve, doctor
    config.py                     # TOML 配置加载 (dataclass SchedulerConfig)
    database.py                   # SQLite ORM (threading.RLock, WAL mode)
    scheduler.py                  # 核心调度引擎 (SchedulerService)
    web.py                        # FastAPI 应用 + API 路由 + SSE
    gpu.py                        # nvidia-smi GPU 查询
    events.py                     # 异步事件发布/订阅 (asyncio.Queue)
    terminal.py                   # PTY 终端会话管理
    system_terminal.py            # nvitop 系统终端服务
    profile_discovery.py          # conda/venv 环境自动发现
    static/                       # Vite 构建产物，由 FastAPI 静态服务
  frontend/                       # React 前端源码
    src/App.tsx                   # 单文件 React 应用
    src/index.css                 # Tailwind + 自定义样式
    src/main.tsx                  # React 入口
  tests/                          # pytest 测试
    test_api.py                   # API 集成测试
    test_scheduler.py             # 调度器单元测试
    test_profile_discovery.py     # 环境发现测试
  deploy/exp-scheduler.service    # systemd 用户服务模板
```

## 常用命令

```bash
# 后端
python scripts/exp_scheduler.py init             # 初始化配置和数据库
python scripts/exp_scheduler.py serve            # 启动 Web 服务 (默认 127.0.0.1:17861)
python scripts/exp_scheduler.py doctor           # 环境诊断

# 前端开发
cd tools/exp-scheduler/frontend
npm run dev                    # Vite 开发服务器 (端口 3000)
npm run build                  # 构建到 src/exp_scheduler_app/static/
npm run lint                   # TypeScript 类型检查

# 测试
cd tools/exp-scheduler
pytest tests/ -v               # 运行全部测试
pytest tests/test_scheduler.py # 调度器单元测试
pytest tests/test_api.py       # API 集成测试
pytest tests/test_dependencies.py # 任务依赖测试
```

## 架构要点

- **调度模型**：单 GPU 单任务，GPU 需通过 N 次连续空闲检测（默认 6×5s=30s）才分配任务
- **双优先级队列**：`urgent` 可抢占 `normal` 任务，抢占使用 SIGINT→5s→SIGTERM→5s→SIGKILL 梯度
- **中断恢复**：服务重启或外部信号杀死的任务自动回到队首
- **OOM 重试**：检测 CUDA OOM / exit code 137/143，可配置重试次数和延迟
- **任务依赖 (DAG)**：`task_dependencies` 表存储依赖边，调度器通过 `are_dependencies_satisfied` 检查所有依赖 `succeeded` 后才调度；递归 CTE 检测循环；删除任务时手动清理依赖边
- **实时通信**：SSE 推送任务状态变更和终端输出，前端用 xterm.js 渲染 PTY 流
- **配置**：TOML 文件 (`~/.config/exp-scheduler/config.toml`)，部分设置可通过 Web UI 运行时修改并持久化到 SQLite meta 表

## 编码规范

- Python: `from __future__ import annotations` 在每个文件开头；使用 `dataclass(slots=True)`；类型注解使用 `str | None` 而非 `Optional[str]`
- 后端无 ORM 库，直接用 `sqlite3` + 手写 SQL，`Database` 类用 `threading.RLock` 保证线程安全
- API 请求/响应用 Pydantic `BaseModel` 定义在 `web.py` 顶部
- 测试中用 `FakeGPUProvider` mock GPU 查询，用 `TestClient` 测试 API
- 前端是单文件 React 应用 (App.tsx)，使用 Tailwind CSS 4 + Motion 动画
- 中文注释和 UI 文本，英文代码标识符

## 添加新 API 端点的模式

1. 在 `web.py` 顶部定义 Pydantic 请求模型
2. 在 `create_app()` 内部的闭包函数中添加路由（依赖 `scheduler`、`db`、`event_broker` 等闭包变量）
3. 操作完成后通过 `event_broker.publish()` 发送 SSE 事件
4. 在 `tests/test_api.py` 中用 `TestClient` 测试

## 添加新数据库字段

1. 在 `database.py` 的 `_ensure_columns()` 方法中添加 `ALTER TABLE ... ADD COLUMN` 语句（自动迁移）
2. 更新对应的 `CREATE TABLE` 语句和查询方法
