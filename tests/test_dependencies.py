"""Tests for task dependency / DAG functionality."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.database import Database
from exp_scheduler_app.gpu import GPUInfo
from exp_scheduler_app.scheduler import SchedulerService
from exp_scheduler_app.web import create_app


def gpu(index: int, *, idle: bool = True) -> GPUInfo:
    return GPUInfo(
        index=index,
        uuid=f"GPU-{index}",
        name=f"Fake GPU {index}",
        memory_total_mb=24564,
        memory_used_mb=500 if idle else 5000,
        utilization_gpu=0 if idle else 82,
        has_processes=not idle,
    )


class FakeGPUProvider:
    def __init__(self, gpus: list[GPUInfo]) -> None:
        self._gpus = gpus

    def set_gpus(self, gpus: list[GPUInfo]) -> None:
        self._gpus = gpus

    def __call__(self) -> list[GPUInfo]:
        return list(self._gpus)


def make_config(tmp_path, **overrides) -> SchedulerConfig:
    state_dir = tmp_path / "state"
    defaults = dict(
        host="127.0.0.1",
        port=17861,
        poll_interval_seconds=0.1,
        gpu_idle_memory_mb=1000,
        gpu_idle_required_checks=1,
        auto_retry_max_retries=0,
        auto_retry_delay_seconds=5,
        state_dir=state_dir,
        log_dir=state_dir / "logs",
    )
    defaults.update(overrides)
    return SchedulerConfig(**defaults)


def make_client(tmp_path, gpu_provider=None):
    config = make_config(tmp_path)
    if gpu_provider is None:
        gpu_provider = FakeGPUProvider([gpu(0)])
    app = create_app(config, gpu_provider=gpu_provider, autostart=False)
    client = TestClient(app)
    return client, config


# ── Database-level tests ──────────────────────────────────────────


def test_add_and_get_dependencies(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])
    deps = db.get_dependencies(t2["id"])
    assert len(deps) == 1
    assert deps[0]["id"] == t1["id"]


def test_get_dependents(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])
    dependents = db.get_dependents(t1["id"])
    assert len(dependents) == 1
    assert dependents[0]["id"] == t2["id"]


def test_are_dependencies_satisfied_no_deps(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    assert db.are_dependencies_satisfied(t1["id"]) is True


def test_are_dependencies_satisfied_all_succeeded(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    t3 = db.create_task(name="C", command="echo c", cwd=None, env={}, notes=None)
    db.add_dependencies(t3["id"], [t1["id"], t2["id"]])
    assert db.are_dependencies_satisfied(t3["id"]) is False
    db.finish_task(task_id=t1["id"], status="succeeded", exit_code=0)
    assert db.are_dependencies_satisfied(t3["id"]) is False
    db.finish_task(task_id=t2["id"], status="succeeded", exit_code=0)
    assert db.are_dependencies_satisfied(t3["id"]) is True


def test_are_dependencies_satisfied_one_failed(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])
    db.finish_task(task_id=t1["id"], status="failed", exit_code=1)
    assert db.are_dependencies_satisfied(t2["id"]) is False


def test_self_dependency_rejected(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    try:
        db.add_dependencies(t1["id"], [t1["id"]])
        assert False, "Should have raised ValueError"
    except ValueError as exc:
        assert "自身" in str(exc)


def test_cycle_detection_simple(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])
    try:
        db.add_dependencies(t1["id"], [t2["id"]])
        assert False, "Should have raised ValueError for cycle"
    except ValueError as exc:
        assert "循环" in str(exc)


def test_cycle_detection_transitive(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    t3 = db.create_task(name="C", command="echo c", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])
    db.add_dependencies(t3["id"], [t2["id"]])
    try:
        db.add_dependencies(t1["id"], [t3["id"]])
        assert False, "Should have raised ValueError for cycle"
    except ValueError as exc:
        assert "循环" in str(exc)


def test_cascade_delete(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])
    db.delete_task(t1["id"])
    deps = db.get_dependencies(t2["id"])
    assert len(deps) == 0
    assert db.are_dependencies_satisfied(t2["id"]) is True


def test_replace_dependencies(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    t3 = db.create_task(name="C", command="echo c", cwd=None, env={}, notes=None)
    db.add_dependencies(t3["id"], [t1["id"], t2["id"]])
    assert len(db.get_dependencies(t3["id"])) == 2
    db.replace_dependencies(t3["id"], [t2["id"]])
    deps = db.get_dependencies(t3["id"])
    assert len(deps) == 1
    assert deps[0]["id"] == t2["id"]


def test_remove_dependencies(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])
    db.remove_dependencies(t2["id"])
    assert len(db.get_dependencies(t2["id"])) == 0


def test_get_dependency_count(tmp_path):
    config = make_config(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    t3 = db.create_task(name="C", command="echo c", cwd=None, env={}, notes=None)
    assert db.get_dependency_count(t3["id"]) == 0
    db.add_dependencies(t3["id"], [t1["id"], t2["id"]])
    assert db.get_dependency_count(t3["id"]) == 2


# ── Scheduler integration tests ──────────────────────────────────


def test_blocked_task_not_scheduled(tmp_path):
    config = make_config(tmp_path)
    gpu_provider = FakeGPUProvider([gpu(0)])
    db = Database(config.db_path)
    db.init()
    scheduler = SchedulerService(config=config, database=db, gpu_provider=gpu_provider)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scheduler.startup())
        # Use a slow command for t1 so we can observe t2 being blocked
        t1 = loop.run_until_complete(
            scheduler.create_task(name="A", command="sleep 30", cwd=None, env={}, notes=None)
        )
        t2 = loop.run_until_complete(
            scheduler.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
        )
        loop.run_until_complete(scheduler.set_task_dependencies(t2["id"], [t1["id"]]))

        # Verify dependency is set
        assert db.are_dependencies_satisfied(t2["id"]) is False

        # Let scheduler tick - t1 should start, t2 should stay queued
        for _ in range(8):
            loop.run_until_complete(asyncio.sleep(0.15))

        t1_status = db.get_task(t1["id"])["status"]
        t2_status = db.get_task(t2["id"])["status"]

        assert t1_status == "running", f"t1 status: {t1_status}"
        assert t2_status == "queued", f"t2 status: {t2_status}"

        # Cancel t1 so the test doesn't hang
        loop.run_until_complete(scheduler.cancel_task(t1["id"]))
        for _ in range(10):
            loop.run_until_complete(asyncio.sleep(0.15))
            if db.get_task(t1["id"])["status"] == "cancelled":
                break

        # t2 should stay queued since dependency was cancelled (not succeeded)
        assert db.get_task(t2["id"])["status"] == "queued"
    finally:
        loop.run_until_complete(scheduler.shutdown())
        loop.close()


def test_failed_dependency_blocks_dependent(tmp_path):
    config = make_config(tmp_path)
    gpu_provider = FakeGPUProvider([gpu(0)])
    db = Database(config.db_path)
    db.init()
    scheduler = SchedulerService(config=config, database=db, gpu_provider=gpu_provider)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scheduler.startup())
        t1 = loop.run_until_complete(
            scheduler.create_task(name="A", command="exit 1", cwd=None, env={}, notes=None)
        )
        t2 = loop.run_until_complete(
            scheduler.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
        )
        loop.run_until_complete(scheduler.set_task_dependencies(t2["id"], [t1["id"]]))

        # Wait for t1 to fail
        for _ in range(30):
            loop.run_until_complete(asyncio.sleep(0.15))
            if db.get_task(t1["id"])["status"] == "failed":
                break

        assert db.get_task(t1["id"])["status"] == "failed"

        # t2 should stay queued because dependency failed
        for _ in range(10):
            loop.run_until_complete(asyncio.sleep(0.15))

        assert db.get_task(t2["id"])["status"] == "queued"
    finally:
        loop.run_until_complete(scheduler.shutdown())
        loop.close()


def test_multiple_dependencies(tmp_path):
    config = make_config(tmp_path)
    gpu_provider = FakeGPUProvider([gpu(0)])
    db = Database(config.db_path)
    db.init()
    scheduler = SchedulerService(config=config, database=db, gpu_provider=gpu_provider)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(scheduler.startup())
        t1 = loop.run_until_complete(
            scheduler.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
        )
        t2 = loop.run_until_complete(
            scheduler.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
        )
        t3 = loop.run_until_complete(
            scheduler.create_task(name="C", command="echo c", cwd=None, env={}, notes=None)
        )
        loop.run_until_complete(scheduler.set_task_dependencies(t3["id"], [t1["id"], t2["id"]]))

        # Wait for t1 and t2 to finish
        for _ in range(40):
            loop.run_until_complete(asyncio.sleep(0.15))
            s1 = db.get_task(t1["id"])["status"]
            s2 = db.get_task(t2["id"])["status"]
            if s1 == "succeeded" and s2 == "succeeded":
                break

        # t3 should eventually run
        for _ in range(20):
            loop.run_until_complete(asyncio.sleep(0.15))
            t3_status = db.get_task(t3["id"])["status"]
            if t3_status in ("running", "succeeded"):
                break

        assert db.get_task(t3["id"])["status"] in ("running", "succeeded")
    finally:
        loop.run_until_complete(scheduler.shutdown())
        loop.close()


# ── API integration tests ─────────────────────────────────────────


def test_create_task_with_dependencies(tmp_path):
    client, config = make_client(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)

    resp = client.post("/api/tasks", json={
        "command": "echo b",
        "depends_on": [t1["id"]],
    })
    assert resp.status_code == 200
    task = resp.json()["task"]
    assert task["depends_on"] == [t1["id"]]
    assert task["dependency_count"] == 1
    assert len(task["dependencies"]) == 1
    assert task["dependencies"][0]["id"] == t1["id"]


def test_create_task_invalid_dependency_does_not_create_task(tmp_path):
    client, config = make_client(tmp_path)
    db = Database(config.db_path)
    db.init()

    resp = client.post("/api/tasks", json={
        "command": "echo orphan",
        "depends_on": [999],
    })
    assert resp.status_code == 400
    assert all(task["command"] != "echo orphan" for task in db.list_tasks()["queued"])


def test_create_task_with_cyclic_dependency(tmp_path):
    client, config = make_client(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)

    # Create t2 depending on t1
    resp = client.post("/api/tasks", json={
        "command": "echo b",
        "depends_on": [t1["id"]],
    })
    assert resp.status_code == 200
    t2_id = resp.json()["task"]["id"]

    # Try to make t1 depend on t2 (cycle)
    resp = client.put(f"/api/tasks/{t1['id']}/dependencies", json={
        "depends_on": [t2_id],
    })
    assert resp.status_code == 400
    assert "循环" in resp.json()["detail"]


def test_get_task_dependencies_endpoint(tmp_path):
    client, config = make_client(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])

    resp = client.get(f"/api/tasks/{t2['id']}/dependencies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["dependencies"]) == 1
    assert data["dependencies"][0]["id"] == t1["id"]
    assert data["dependencies_satisfied"] is False

    resp = client.get(f"/api/tasks/{t1['id']}/dependencies")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["dependents"]) == 1
    assert data["dependents"][0]["id"] == t2["id"]


def test_update_task_dependencies_endpoint(tmp_path):
    client, config = make_client(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)

    # Set dependency
    resp = client.put(f"/api/tasks/{t2['id']}/dependencies", json={
        "depends_on": [t1["id"]],
    })
    assert resp.status_code == 200
    assert len(db.get_dependencies(t2["id"])) == 1

    # Clear dependency
    resp = client.put(f"/api/tasks/{t2['id']}/dependencies", json={
        "depends_on": [],
    })
    assert resp.status_code == 200
    assert len(db.get_dependencies(t2["id"])) == 0


def test_update_task_without_depends_on_preserves_dependencies(tmp_path):
    client, config = make_client(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])

    resp = client.put(f"/api/tasks/{t2['id']}", json={
        "command": "echo updated",
    })
    assert resp.status_code == 200
    assert [dep["id"] for dep in db.get_dependencies(t2["id"])] == [t1["id"]]
    assert resp.json()["task"]["depends_on"] == [t1["id"]]


def test_duplicate_dependency_ids_are_deduplicated(tmp_path):
    client, config = make_client(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)

    resp = client.put(f"/api/tasks/{t2['id']}/dependencies", json={
        "depends_on": [t1["id"], t1["id"]],
    })
    assert resp.status_code == 200
    assert [dep["id"] for dep in db.get_dependencies(t2["id"])] == [t1["id"]]


def test_delete_task_with_dependents(tmp_path):
    client, config = make_client(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])

    # Delete t1 - should cascade remove dependency edges
    resp = client.delete(f"/api/tasks/{t1['id']}")
    assert resp.status_code == 200

    # t2 should have no dependencies now
    assert len(db.get_dependencies(t2["id"])) == 0
    assert db.are_dependencies_satisfied(t2["id"]) is True


def test_list_tasks_includes_dependency_info(tmp_path):
    client, config = make_client(tmp_path)
    db = Database(config.db_path)
    db.init()
    t1 = db.create_task(name="A", command="echo a", cwd=None, env={}, notes=None)
    t2 = db.create_task(name="B", command="echo b", cwd=None, env={}, notes=None)
    db.add_dependencies(t2["id"], [t1["id"]])

    resp = client.get("/api/tasks")
    assert resp.status_code == 200
    data = resp.json()

    # Find t2 in the queued list
    all_tasks = data.get("queued", []) + data.get("urgent_queued", [])
    t2_data = next(t for t in all_tasks if t["id"] == t2["id"])
    assert t2_data["has_dependencies"] is True
    assert t2_data["dependency_count"] == 1
    assert t2_data["depends_on"] == [t1["id"]]

    # t1 should have no dependencies
    t1_data = next(t for t in all_tasks if t["id"] == t1["id"])
    assert t1_data["has_dependencies"] is False
    assert t1_data["dependency_count"] == 0
