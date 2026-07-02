from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.database import Database
from exp_scheduler_app.events import EventBroker
from exp_scheduler_app.backup import BackupService
from exp_scheduler_app.file_browser import FileBrowserService
from exp_scheduler_app.nodes import ResolvedAuth


LOCAL_AUTH = ResolvedAuth(
    node_id="local",
    name="本机",
    is_local=True,
    host=None,
    port=0,
    username=None,
    auth_method=None,
    key_path=None,
    password=None,
)


def local_resolver(node_id: str) -> ResolvedAuth:
    if node_id != "local":
        raise ValueError(f"节点不存在: {node_id}")
    return LOCAL_AUTH


def make_config(tmp_path: Path) -> SchedulerConfig:
    state_dir = tmp_path / "state"
    return SchedulerConfig(
        host="127.0.0.1",
        port=17861,
        state_dir=state_dir,
        log_dir=state_dir / "logs",
        terminal_log_dir=state_dir / "terminals",
    )


def make_backup_service(tmp_path: Path) -> BackupService:
    config = make_config(tmp_path)
    db = Database(tmp_path / "test.db")
    db.init()
    return BackupService(
        config=config,
        database=db,
        events=EventBroker(),
        node_resolver=local_resolver,
        known_hosts_path=tmp_path / "known_hosts",
    )


# ---------------------------------------------------------------------------
# FileBrowserService
# ---------------------------------------------------------------------------


def test_file_browser_lists_home_directory(tmp_path):
    svc = FileBrowserService(
        node_resolver=local_resolver,
        known_hosts_path=tmp_path / "known_hosts",
    )
    result = asyncio.run(svc.list_directory("local", "~"))
    assert "path" in result
    assert "entries" in result
    assert isinstance(result["entries"], list)
    assert all("name" in e and "type" in e for e in result["entries"])


def test_file_browser_lists_specific_directory(tmp_path):
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "file.txt").write_text("test")
    (tmp_path / "subdir" / "nested").mkdir()

    svc = FileBrowserService(
        node_resolver=local_resolver,
        known_hosts_path=tmp_path / "known_hosts",
    )
    result = asyncio.run(svc.list_directory("local", str(tmp_path / "subdir")))
    names = {e["name"] for e in result["entries"]}
    assert "file.txt" in names
    assert "nested" in names


def test_file_browser_rejects_nonexistent_path(tmp_path):
    svc = FileBrowserService(
        node_resolver=local_resolver,
        known_hosts_path=tmp_path / "known_hosts",
    )
    with pytest.raises(ValueError, match="路径不存在"):
        asyncio.run(svc.list_directory("local", str(tmp_path / "nonexistent")))


def test_file_browser_rejects_non_directory(tmp_path):
    (tmp_path / "file.txt").write_text("test")
    svc = FileBrowserService(
        node_resolver=local_resolver,
        known_hosts_path=tmp_path / "known_hosts",
    )
    with pytest.raises(ValueError, match="不是目录"):
        asyncio.run(svc.list_directory("local", str(tmp_path / "file.txt")))


def test_file_browser_rejects_unknown_node(tmp_path):
    svc = FileBrowserService(
        node_resolver=local_resolver,
        known_hosts_path=tmp_path / "known_hosts",
    )
    with pytest.raises(ValueError, match="节点不存在"):
        asyncio.run(svc.list_directory("ghost", "~"))


# ---------------------------------------------------------------------------
# BackupService
# ---------------------------------------------------------------------------


def test_backup_create_and_manual_run(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            src = tmp_path / "src"
            src.mkdir()
            (src / "file1.txt").write_text("hello")
            dst = tmp_path / "dst"

            job = await svc.create_job(
                name="test-backup",
                src_node_id="local",
                src_path=str(src),
                dst_node_id="local",
                dst_path=str(dst),
                schedule_type="manual",
            )
            assert job["name"] == "test-backup"
            assert job["schedule_type"] == "manual"

            run = await svc.trigger_run(job["id"])
            run_id = int(run["id"])

            deadline = asyncio.get_event_loop().time() + 15
            while True:
                r = svc.database.get_backup_run(run_id)
                if r and r["status"] != "running":
                    break
                if asyncio.get_event_loop().time() > deadline:
                    pytest.fail("Backup run timed out")
                await asyncio.sleep(0.5)

            assert r["status"] == "succeeded"
            assert (dst / "file1.txt").read_text() == "hello"
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_backup_incremental(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            src = tmp_path / "src"
            src.mkdir()
            (src / "file1.txt").write_text("v1")
            dst = tmp_path / "dst"

            job = await svc.create_job(
                name="incremental-test",
                src_node_id="local",
                src_path=str(src),
                dst_node_id="local",
                dst_path=str(dst),
                schedule_type="manual",
            )

            run1 = await svc.trigger_run(job["id"])
            run1_id = int(run1["id"])
            deadline = asyncio.get_event_loop().time() + 15
            while True:
                r = svc.database.get_backup_run(run1_id)
                if r and r["status"] != "running":
                    break
                if asyncio.get_event_loop().time() > deadline:
                    pytest.fail("First backup timed out")
                await asyncio.sleep(0.5)
            assert r["status"] == "succeeded"
            assert (dst / "file1.txt").read_text() == "v1"

            await asyncio.sleep(1.1)
            (src / "file1.txt").write_text("v2")
            (src / "file2.txt").write_text("new")

            run2 = await svc.trigger_run(job["id"])
            run2_id = int(run2["id"])
            deadline = asyncio.get_event_loop().time() + 15
            while True:
                r2 = svc.database.get_backup_run(run2_id)
                if r2 and r2["status"] != "running":
                    break
                if asyncio.get_event_loop().time() > deadline:
                    pytest.fail("Incremental backup timed out")
                await asyncio.sleep(0.5)
            assert r2["status"] == "succeeded"
            assert (dst / "file1.txt").read_text() == "v2"
            assert (dst / "file2.txt").read_text() == "new"
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_backup_daily_schedule_sets_next_run(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            job = await svc.create_job(
                name="daily-test",
                src_node_id="local",
                src_path="/tmp",
                dst_node_id="local",
                dst_path="/tmp/dst",
                schedule_type="daily",
                schedule_hour=3,
                schedule_minute=30,
            )
            assert job["next_run_at"] is not None
            assert job["schedule_type"] == "daily"
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_backup_weekly_schedule_sets_next_run(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            job = await svc.create_job(
                name="weekly-test",
                src_node_id="local",
                src_path="/tmp",
                dst_node_id="local",
                dst_path="/tmp/dst",
                schedule_type="weekly",
                schedule_hour=2,
                schedule_minute=0,
                schedule_day_of_week=0,
            )
            assert job["next_run_at"] is not None
            assert job["schedule_day_of_week"] == 0
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_backup_update_job(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            job = await svc.create_job(
                name="update-test",
                src_node_id="local",
                src_path="/tmp",
                dst_node_id="local",
                dst_path="/tmp/dst",
            )
            updated = await svc.update_job(job["id"], enabled=False, name="renamed")
            assert updated["enabled"] is False
            assert updated["name"] == "renamed"
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_backup_delete_job(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            job = await svc.create_job(
                name="delete-test",
                src_node_id="local",
                src_path="/tmp",
                dst_node_id="local",
                dst_path="/tmp/dst",
            )
            ok = await svc.delete_job(job["id"])
            assert ok is True
            assert await svc.get_job(job["id"]) is None
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_backup_rejects_empty_name(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            with pytest.raises(ValueError, match="不能为空"):
                await svc.create_job(
                    name="  ",
                    src_node_id="local",
                    src_path="/tmp",
                    dst_node_id="local",
                    dst_path="/tmp/dst",
                )
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_backup_rejects_empty_paths(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            with pytest.raises(ValueError, match="不能为空"):
                await svc.create_job(
                    name="test",
                    src_node_id="local",
                    src_path="",
                    dst_node_id="local",
                    dst_path="/tmp/dst",
                )
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_backup_rejects_invalid_schedule_type(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            with pytest.raises(ValueError, match="无效的调度类型"):
                await svc.create_job(
                    name="test",
                    src_node_id="local",
                    src_path="/tmp",
                    dst_node_id="local",
                    dst_path="/tmp/dst",
                    schedule_type="hourly",
                )
        finally:
            await svc.shutdown()

    asyncio.run(scenario())


def test_backup_list_runs(tmp_path):
    async def scenario():
        svc = make_backup_service(tmp_path)
        await svc.startup()
        try:
            src = tmp_path / "src"
            src.mkdir()
            (src / "f.txt").write_text("x")
            dst = tmp_path / "dst"

            job = await svc.create_job(
                name="runs-test",
                src_node_id="local",
                src_path=str(src),
                dst_node_id="local",
                dst_path=str(dst),
            )
            run = await svc.trigger_run(job["id"])
            run_id = int(run["id"])
            deadline = asyncio.get_event_loop().time() + 15
            while True:
                r = svc.database.get_backup_run(run_id)
                if r and r["status"] != "running":
                    break
                if asyncio.get_event_loop().time() > deadline:
                    pytest.fail("Backup timed out")
                await asyncio.sleep(0.5)

            runs = await svc.list_runs(job["id"])
            assert len(runs) >= 1
            assert any(r["id"] == run_id for r in runs)
        finally:
            await svc.shutdown()

    asyncio.run(scenario())
