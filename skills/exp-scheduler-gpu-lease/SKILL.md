---
name: exp-scheduler-gpu-lease
description: "Control the local exp-scheduler service from Codex or another agent: reserve and release GPUs through agent GPU leases, run commands under temporary GPU leases, create normal or urgent scheduler tasks, list/reorder queues, cancel/delete/requeue/preempt tasks, update task metadata/dependencies, inspect profiles, and pause/resume scheduling when explicitly requested. Use when an agent needs to run tests without disrupting unrelated GPUs, enqueue experiments, or manage exp-scheduler tasks through its HTTP API."
---

# exp-scheduler Agent Control

## Overview

Use the bundled wrapper script to operate exp-scheduler safely from an agent. It wraps the local HTTP API for GPU leases, task queue management, profile inspection, and selected global queue controls.

Default service URL: `http://127.0.0.1:17861`. Override with `--base-url` or `EXP_SCHEDULER_URL`.

Script path:

```bash
"${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py"
```

## Safety Rules

- Do not use `queue-pause` for GPU-specific tests; it affects the whole queue. Use a GPU lease instead.
- Use a finite TTL unless the user explicitly asks for a persistent lease.
- Treat `stop_running` as destructive to the current process: it interrupts scheduler-managed tasks on the leased GPUs and puts them back at the queue head. It is not checkpoint/resume.
- Always release leases after direct agent tests. Prefer `run` mode so release happens in a `finally` block.
- If a command should use the leased GPU directly, set `CUDA_VISIBLE_DEVICES` to the leased physical GPU IDs. The wrapper does this automatically in `run` mode unless `--no-cuda-visible-devices` is passed.
- `task-delete` deletes queued/history records and logs. Use `task-cancel` for running tasks.
- `task-reorder` replaces the complete order of one queue; list tasks first and include every task ID from that queue.
- Use `--urgent` on `task-create` for urgent queue tasks. Urgent tasks do not automatically kill running work; use `task-preempt` only when urgent work is queued and the user expects preemption.

## Inspect State

Use `status` for the compact operational picture:

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" status
```

Use `task-list` when queue order or history matters:

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" task-list
```

Use `profile-list` before creating tasks with environment profiles:

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" profile-list
```

## GPU Lease Workflows

Run a direct test under an automatic lease:

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" run \
  --gpu 2 \
  --owner codex-test \
  --ttl 3600 \
  --stop-running \
  -- pytest -q
```

If a manual lease is needed, acquire and later release:

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" acquire \
  --gpu 2 \
  --owner codex-test \
  --ttl 3600 \
  --stop-running
```

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" release <lease_id>
```

## Choosing `stop_running`

Use `--stop-running` when the user expects the agent to take over that GPU now, or when a test cannot share the GPU. Leave it off when the agent only needs to prevent future scheduler launches and can wait for current work to finish.

Before using `--stop-running`, inspect `status` when feasible and mention that only tasks on the requested GPUs are interrupted.

## Task Workflows

Create a normal task:

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" task-create \
  --name smoke \
  --cwd /path/to/project \
  --env PYTHONUNBUFFERED=1 \
  --command 'python smoke.py'
```

Create an urgent task pinned to GPU 2:

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" task-create \
  --name urgent-eval \
  --urgent \
  --requested-gpu 2 \
  --memory-budget-mb 16000 \
  --profile-id 3 \
  --command 'python eval.py'
```

Reorder a queue after listing it. The IDs must be the full queue order:

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" task-reorder \
  --queue urgent 42 39 44
```

Common task commands:

- `task-update <task_id> --command ...`: update a queued task with a full task payload, using the same options as `task-create`.
- `task-delete <task_id>`: delete queued/history task records; running tasks are rejected by the server.
- `task-cancel <task_id>`: cancel a running task.
- `task-requeue <task_id>`: clone failed/cancelled/interrupted tasks back into the queue.
- `task-preempt <task_id>`: interrupt a running task and put it at normal queue head so queued urgent work runs first.
- `task-metadata <task_id> --name ... --notes ...`: update record metadata.
- `task-dependencies get <task_id>` and `task-dependencies set <task_id> <dep_id>...`: inspect or replace dependencies.

## Global Queue Controls

Only use global pause/resume when explicitly requested:

```bash
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" queue-pause
python "${CODEX_HOME:-$HOME/.codex}/skills/exp-scheduler-gpu-lease/scripts/exp_scheduler_gpu_lease.py" queue-resume
```

`queue-pause --stop-running` interrupts all scheduler-managed running tasks and requeues them. For one GPU, use lease `--stop-running` instead.

## Failure Handling

- If the service is unreachable, run or ask the user to run `exp-scheduler serve`, or check the SSH tunnel if accessing remotely.
- If a GPU is invalid, inspect `status` and choose a listed GPU ID.
- If `run` fails, the command exit code is preserved after release. Report both the command failure and whether release succeeded.
- If release fails, print or report the lease ID so the user can release it manually.
- If an API call returns HTTP 409, the requested operation conflicts with task state; list tasks and choose the state-appropriate command.
