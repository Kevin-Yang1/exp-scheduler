#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_BASE_URL = "http://127.0.0.1:17861"
URL_OPENER = build_opener(ProxyHandler({}))
TASK_LISTS = ("queued", "urgent_queued", "running", "history")


class ApiError(RuntimeError):
    pass


def request_json(
    method: str,
    path: str,
    *,
    base_url: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with URL_OPENER.open(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ApiError(f"{method} {url} failed: {exc.reason}") from exc
    if not raw:
        return {}
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(f"{method} {url} returned non-JSON response: {raw[:200]}") from exc
    if not isinstance(result, dict):
        raise ApiError(f"{method} {url} returned unexpected JSON: {raw[:200]}")
    return result


def build_query(path: str, params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value is not None}
    if not clean:
        return path
    return f"{path}?{urlencode(clean)}"


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def parse_json_value(value: str) -> Any:
    raw = Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value
    return json.loads(raw)


def parse_json_object(value: str, *, label: str) -> dict[str, Any]:
    try:
        payload = parse_json_value(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise argparse.ArgumentTypeError(f"{label} must be JSON object or @file") from exc
    if not isinstance(payload, dict):
        raise argparse.ArgumentTypeError(f"{label} must be a JSON object")
    return payload


def positive_float_or_none(value: str) -> float | None:
    if value.lower() in {"none", "null", "never"}:
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("TTL must be a number, or null") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("TTL must be greater than 0, or null")
    return result


def parse_gpu_ids(values: list[int]) -> list[int]:
    seen: set[int] = set()
    gpu_ids: list[int] = []
    for value in values:
        gpu_id = int(value)
        if gpu_id < 0:
            raise argparse.ArgumentTypeError("GPU IDs must be non-negative")
        if gpu_id in seen:
            continue
        seen.add(gpu_id)
        gpu_ids.append(gpu_id)
    if not gpu_ids:
        raise argparse.ArgumentTypeError("at least one --gpu is required")
    return gpu_ids


def parse_key_values(values: list[str] | None, *, label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise argparse.ArgumentTypeError(f"{label} must use KEY=VALUE: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise argparse.ArgumentTypeError(f"{label} key cannot be empty")
        parsed[key] = value
    return parsed


def parse_env(args: argparse.Namespace) -> dict[str, str]:
    env: dict[str, str] = {}
    if getattr(args, "env_json", None):
        raw_env = parse_json_object(args.env_json, label="--env-json")
        env.update({str(key): str(value) for key, value in raw_env.items()})
    env.update(parse_key_values(getattr(args, "env", None), label="--env"))
    return env


def depends_on_ids(args: argparse.Namespace) -> list[int]:
    return [int(task_id) for task_id in getattr(args, "depends_on", None) or []]


def task_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": args.name,
        "command": args.task_command,
        "cwd": args.cwd,
        "env": parse_env(args),
        "notes": args.notes,
        "is_urgent": bool(args.urgent),
        "requested_gpu": args.requested_gpu,
        "gpu_memory_budget_mb": args.memory_budget_mb,
        "profile_id": args.profile_id,
        "depends_on": depends_on_ids(args),
    }
    return payload


def cmd_status(args: argparse.Namespace) -> int:
    settings = request_json("GET", "/api/settings", base_url=args.base_url)
    gpus = request_json("GET", "/api/gpus", base_url=args.base_url)
    leases = request_json(
        "GET",
        "/api/agent/gpu-leases?include_inactive=true",
        base_url=args.base_url,
    )
    tasks = request_json(
        "GET",
        "/api/tasks?history_limit=20",
        base_url=args.base_url,
    )
    print_json(
        {
            "settings": settings,
            "gpus": gpus.get("gpus", []),
            "leases": leases.get("leases", []),
            "tasks": {
                "queue_paused": tasks.get("queue_paused"),
                "counts": tasks.get("counts", {}),
                "queued": tasks.get("queued", []),
                "urgent_queued": tasks.get("urgent_queued", []),
                "running": tasks.get("running", []),
            },
        }
    )
    return 0


def acquire_lease(args: argparse.Namespace) -> dict[str, Any]:
    gpu_ids = parse_gpu_ids(args.gpu)
    payload = {
        "owner": args.owner,
        "gpu_ids": gpu_ids,
        "ttl_seconds": args.ttl,
        "stop_running": bool(args.stop_running),
        "notes": args.notes,
    }
    return request_json("POST", "/api/agent/gpu-leases", base_url=args.base_url, payload=payload)


def cmd_acquire(args: argparse.Namespace) -> int:
    print_json(acquire_lease(args))
    return 0


def release_lease(base_url: str, lease_id: str) -> dict[str, Any]:
    return request_json("DELETE", f"/api/agent/gpu-leases/{lease_id}", base_url=base_url)


def cmd_release(args: argparse.Namespace) -> int:
    print_json(release_lease(args.base_url, args.lease_id))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        print("run requires a command after --", file=sys.stderr)
        return 2
    result = acquire_lease(args)
    lease = result.get("lease") if isinstance(result.get("lease"), dict) else {}
    lease_id = str(lease.get("id") or "")
    gpu_ids = parse_gpu_ids(args.gpu)
    print_json(result)
    if not lease_id:
        print("lease response did not include lease.id", file=sys.stderr)
        return 1

    env = os.environ.copy()
    if not args.no_cuda_visible_devices:
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(gpu_id) for gpu_id in gpu_ids)

    command_result = 1
    release_error: Exception | None = None
    try:
        completed = subprocess.run(args.command, env=env, check=False)
        command_result = int(completed.returncode)
    finally:
        try:
            release_result = release_lease(args.base_url, lease_id)
            print_json({"released": release_result})
        except Exception as exc:  # noqa: BLE001 - preserve command exit while reporting release failure.
            release_error = exc
            print(f"failed to release lease {lease_id}: {exc}", file=sys.stderr)

    if release_error is not None and command_result == 0:
        return 1
    return command_result


def cmd_task_list(args: argparse.Namespace) -> int:
    path = build_query(
        "/api/tasks",
        {
            "history_limit": args.history_limit,
            "history_offset": args.history_offset,
            "history_sort": args.history_sort,
            "history_status": args.history_status,
        },
    )
    print_json(request_json("GET", path, base_url=args.base_url))
    return 0


def cmd_task_create(args: argparse.Namespace) -> int:
    print_json(
        request_json(
            "POST",
            "/api/tasks",
            base_url=args.base_url,
            payload=task_payload(args),
        )
    )
    return 0


def cmd_task_update(args: argparse.Namespace) -> int:
    print_json(
        request_json(
            "PUT",
            f"/api/tasks/{args.task_id}",
            base_url=args.base_url,
            payload=task_payload(args),
        )
    )
    return 0


def cmd_task_delete(args: argparse.Namespace) -> int:
    print_json(request_json("DELETE", f"/api/tasks/{args.task_id}", base_url=args.base_url))
    return 0


def cmd_task_cancel(args: argparse.Namespace) -> int:
    print_json(request_json("POST", f"/api/tasks/{args.task_id}/cancel", base_url=args.base_url))
    return 0


def cmd_task_requeue(args: argparse.Namespace) -> int:
    print_json(request_json("POST", f"/api/tasks/{args.task_id}/requeue", base_url=args.base_url))
    return 0


def cmd_task_preempt(args: argparse.Namespace) -> int:
    print_json(request_json("POST", f"/api/tasks/{args.task_id}/preempt", base_url=args.base_url))
    return 0


def cmd_task_reorder(args: argparse.Namespace) -> int:
    payload = {"task_ids": [int(task_id) for task_id in args.task_ids], "queue_name": args.queue}
    print_json(
        request_json(
            "POST",
            "/api/tasks/reorder",
            base_url=args.base_url,
            payload=payload,
        )
    )
    return 0


def cmd_task_metadata(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {}
    if args.name is not None:
        payload["name"] = args.name
    if args.notes is not None:
        payload["notes"] = args.notes
    if not payload:
        print("task-metadata requires --name and/or --notes", file=sys.stderr)
        return 2
    print_json(
        request_json(
            "PATCH",
            f"/api/tasks/{args.task_id}/metadata",
            base_url=args.base_url,
            payload=payload,
        )
    )
    return 0


def cmd_dependencies(args: argparse.Namespace) -> int:
    if args.dependency_command == "get":
        print_json(
            request_json(
                "GET",
                f"/api/tasks/{args.task_id}/dependencies",
                base_url=args.base_url,
            )
        )
        return 0
    if args.dependency_command == "set":
        payload = {"depends_on": [int(task_id) for task_id in args.depends_on]}
        print_json(
            request_json(
                "PUT",
                f"/api/tasks/{args.task_id}/dependencies",
                base_url=args.base_url,
                payload=payload,
            )
        )
        return 0
    raise ApiError(f"unknown dependency command: {args.dependency_command}")


def cmd_queue_pause(args: argparse.Namespace) -> int:
    print_json(
        request_json(
            "POST",
            "/api/queue/pause",
            base_url=args.base_url,
            payload={"stop_running": bool(args.stop_running)},
        )
    )
    return 0


def cmd_queue_resume(args: argparse.Namespace) -> int:
    print_json(request_json("POST", "/api/queue/resume", base_url=args.base_url))
    return 0


def cmd_profile_list(args: argparse.Namespace) -> int:
    print_json(request_json("GET", "/api/profiles", base_url=args.base_url))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Agent control helper for exp-scheduler tasks and GPU leases."
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("EXP_SCHEDULER_URL", DEFAULT_BASE_URL),
        help=f"exp-scheduler URL, default: {DEFAULT_BASE_URL}",
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    status = subparsers.add_parser("status", help="Show settings, GPUs, leases, and queue summary")
    status.set_defaults(func=cmd_status)

    acquire = subparsers.add_parser("acquire", help="Create a manual GPU lease")
    add_lease_args(acquire)
    acquire.set_defaults(func=cmd_acquire)

    release = subparsers.add_parser("release", help="Release a GPU lease")
    release.add_argument("lease_id")
    release.set_defaults(func=cmd_release)

    run = subparsers.add_parser("run", help="Acquire a GPU lease, run a command, then release")
    add_lease_args(run)
    run.add_argument(
        "--no-cuda-visible-devices",
        action="store_true",
        help="Do not set CUDA_VISIBLE_DEVICES for the child command",
    )
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    task_list = subparsers.add_parser("task-list", help="List queued, running, and history tasks")
    task_list.add_argument("--history-limit", type=int, default=100)
    task_list.add_argument("--history-offset", type=int, default=0)
    task_list.add_argument("--history-sort", choices=["finished_at", "started_at"], default="finished_at")
    task_list.add_argument(
        "--history-status",
        choices=["succeeded", "failed", "cancelled", "interrupted"],
    )
    task_list.set_defaults(func=cmd_task_list)

    task_create = subparsers.add_parser("task-create", help="Create a queued task")
    add_task_payload_args(task_create)
    task_create.set_defaults(func=cmd_task_create)

    task_update = subparsers.add_parser("task-update", help="Update a queued task with a full task payload")
    task_update.add_argument("task_id", type=int)
    add_task_payload_args(task_update)
    task_update.set_defaults(func=cmd_task_update)

    task_delete = subparsers.add_parser("task-delete", help="Delete a queued or history task")
    task_delete.add_argument("task_id", type=int)
    task_delete.set_defaults(func=cmd_task_delete)

    task_cancel = subparsers.add_parser("task-cancel", help="Cancel a running task")
    task_cancel.add_argument("task_id", type=int)
    task_cancel.set_defaults(func=cmd_task_cancel)

    task_requeue = subparsers.add_parser("task-requeue", help="Clone a failed/cancelled/interrupted task back to queue")
    task_requeue.add_argument("task_id", type=int)
    task_requeue.set_defaults(func=cmd_task_requeue)

    task_preempt = subparsers.add_parser("task-preempt", help="Preempt a running task for urgent queued work")
    task_preempt.add_argument("task_id", type=int)
    task_preempt.set_defaults(func=cmd_task_preempt)

    task_reorder = subparsers.add_parser("task-reorder", help="Replace queue order with the given task IDs")
    task_reorder.add_argument("--queue", choices=["normal", "urgent"], default="normal")
    task_reorder.add_argument("task_ids", nargs="+", type=int)
    task_reorder.set_defaults(func=cmd_task_reorder)

    task_metadata = subparsers.add_parser("task-metadata", help="Update task name and/or notes")
    task_metadata.add_argument("task_id", type=int)
    task_metadata.add_argument("--name")
    task_metadata.add_argument("--notes")
    task_metadata.set_defaults(func=cmd_task_metadata)

    task_deps = subparsers.add_parser("task-dependencies", help="Get or replace task dependencies")
    dep_subparsers = task_deps.add_subparsers(dest="dependency_command", required=True)
    dep_get = dep_subparsers.add_parser("get")
    dep_get.add_argument("task_id", type=int)
    dep_get.set_defaults(func=cmd_dependencies)
    dep_set = dep_subparsers.add_parser("set")
    dep_set.add_argument("task_id", type=int)
    dep_set.add_argument("depends_on", nargs="*", type=int)
    dep_set.set_defaults(func=cmd_dependencies)

    queue_pause = subparsers.add_parser("queue-pause", help="Globally pause scheduling")
    queue_pause.add_argument("--stop-running", action="store_true")
    queue_pause.set_defaults(func=cmd_queue_pause)

    queue_resume = subparsers.add_parser("queue-resume", help="Resume global scheduling")
    queue_resume.set_defaults(func=cmd_queue_resume)

    profile_list = subparsers.add_parser("profile-list", help="List environment profiles")
    profile_list.set_defaults(func=cmd_profile_list)

    return parser


def add_lease_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--gpu", type=int, action="append", required=True, help="GPU ID to lease")
    parser.add_argument("--owner", required=True, help="Agent or caller name")
    parser.add_argument(
        "--ttl",
        type=positive_float_or_none,
        default=3600.0,
        help="Lease TTL in seconds; use null for no automatic expiry",
    )
    parser.add_argument(
        "--stop-running",
        action="store_true",
        help="Interrupt scheduler tasks on the leased GPUs and requeue them",
    )
    parser.add_argument("--notes", help="Optional lease notes")


def add_task_payload_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--command", dest="task_command", required=True, help="Shell command to run")
    parser.add_argument("--name")
    parser.add_argument("--cwd")
    parser.add_argument("--env", action="append", help="Environment variable as KEY=VALUE; repeatable")
    parser.add_argument("--env-json", help="JSON object or @file for environment variables")
    parser.add_argument("--notes")
    parser.add_argument("--urgent", action="store_true", help="Add task to urgent queue")
    parser.add_argument("--requested-gpu", type=int, help="Pin task to a physical GPU ID")
    parser.add_argument("--memory-budget-mb", type=int, help="GPU memory budget in MB")
    parser.add_argument("--profile-id", type=int, help="Environment profile ID")
    parser.add_argument("--depends-on", type=int, action="append", help="Dependency task ID; repeatable")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ApiError, argparse.ArgumentTypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
