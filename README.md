# exp-scheduler

GPU 实验任务调度器。它会持续观察服务器上的 GPU 空闲情况，把你提交的命令按队列顺序自动运行，并提供一个适合通过 SSH 隧道访问的 Web 界面。

## 特性
- 单任务独占单卡调度
- 支持每个任务单独指定 GPU，默认自动分配
- 支持设置全局可调度 GPU 白名单，默认全部可用且可在页面实时修改
- 支持紧急任务队列，以及把运行中的任务抢占回普通队列队首
- SQLite 持久化队列和历史
- FastAPI Web 服务 + 原生 HTML/CSS/JS 前端
- 支持新增、删除、重排、取消、重新入队、暂停/恢复调度
- 支持在界面头部显示当前服务器名称和 IP，方便多机区分
- 支持按 `wait_and_run.sh` 风格对 OOM / 资源类错误自动重试，且由全局配置统一控制
- 每个任务独立日志，运行中任务以只读终端实时查看，历史任务保留纯文本日志视图

## 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r tools/exp-scheduler/requirements-dev.txt
```

## 初始化

```bash
./scripts/exp_scheduler.py init
```

## 启动服务

```bash
./scripts/exp_scheduler.py serve
```

## 浏览器访问

在本机执行：

```bash
ssh -L 17861:127.0.0.1:17861 <server>
```

然后打开：

```text
http://127.0.0.1:17861
```

## 日志查看

运行中任务会以只读 PTY 终端形式展示，因此颜色、进度条和覆盖刷新类输出都能正常显示。任务结束后，页面会自动切回历史纯文本日志视图；浏览器侧在 v1 不支持任意键盘输入，只提供查看能力。
