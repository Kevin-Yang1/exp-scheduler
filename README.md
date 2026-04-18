# exp-scheduler

GPU 实验任务调度器。它会持续观察服务器上的 GPU 空闲情况，把你提交的命令按队列顺序自动运行，并提供一个适合通过 SSH 隧道访问的 Web 界面。

## 特性
- 单任务独占单卡调度
- SQLite 持久化队列和历史
- FastAPI Web 服务 + 原生 HTML/CSS/JS 前端
- 支持新增、删除、重排、取消、重新入队、暂停/恢复调度
- 支持在界面头部显示当前服务器名称和 IP，方便多机区分
- 支持按 `wait_and_run.sh` 风格对 OOM / 资源类错误自动重试，且由全局配置统一控制
- 每个任务独立日志，页面可实时查看

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
