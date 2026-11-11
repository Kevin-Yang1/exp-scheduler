import os
import stat
import tomllib

from fastapi.testclient import TestClient
import pytest

from exp_scheduler_app.config import load_config, persist_server_name
from exp_scheduler_app.web import create_app


def settings_app(config):
    app = create_app(config, autostart=False, gpu_provider=lambda: [])
    app.state.scheduler.database.init()
    return app


def config_file(tmp_path, name_line=""):
    path = tmp_path / "custom" / "scheduler.toml"
    path.parent.mkdir()
    path.write_text(
        '# Keep this comment\n'
        f'{name_line}'
        'server_ip = "10.20.30.40"\n'
        f'state_dir = "{tmp_path / "state"}"\n'
        'poll_interval_seconds = 7\n'
        '[extra]\nserver_name = "nested value"\n',
        encoding="utf-8",
    )
    path.chmod(0o640)
    return path


@pytest.mark.parametrize("name_line", [
    "", 'server_name = "old" # old name\n',
    "'server_name' = 'old'\n", 'server_name = """old\nname"""\n',
])
def test_server_name_persists_in_actual_config_and_preserves_other_values(tmp_path, name_line):
    path = config_file(tmp_path, name_line)
    before = tomllib.loads(path.read_text())
    config = load_config(path)
    assert config.source_path == path.resolve()
    persist_server_name(config, '  实验 "A" \\ GPU 服务器  ')
    after = tomllib.loads(path.read_text())
    assert after == before | {"server_name": '实验 "A" \\ GPU 服务器'}
    assert '# Keep this comment' in path.read_text()
    assert '[extra]\nserver_name = "nested value"\n' in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert load_config(path).server_name == '实验 "A" \\ GPU 服务器'
    assert not (config.state_dir / "config.toml").exists()
    assert not list(path.parent.glob(f".{path.name}.*"))


def test_api_server_name_updates_immediately_and_survives_new_app(tmp_path):
    path = config_file(tmp_path, 'server_name = "original"\n')
    config = load_config(path)
    app = settings_app(config)
    with TestClient(app) as client:
        response = client.patch("/api/server", json={"server_name": "  实验服务器 A  "})
        assert response.status_code == 200
        assert response.json() == {
            "server_name": "实验服务器 A", "server_ip": "10.20.30.40", "host": "127.0.0.1", "port": 17861,
        }
        assert client.get("/api/server").json()["server_name"] == "实验服务器 A"
        assert app.state.node_registry.server_name == "实验服务器 A"
    reloaded = settings_app(load_config(path))
    with TestClient(reloaded) as client:
        assert client.get("/api/server").json()["server_name"] == "实验服务器 A"


@pytest.mark.parametrize("payload", [
    {}, {"server_name": ""}, {"server_name": "   "}, {"server_name": "a\nb"},
    {"server_name": "a" * 81}, {"server_name": None}, {"server_name": 123},
    {"server_name": "valid", "server_ip": "1.2.3.4"},
])
def test_invalid_server_name_does_not_change_disk_or_memory(tmp_path, payload):
    path = config_file(tmp_path, 'server_name = "original"\n')
    original = path.read_bytes()
    app = settings_app(load_config(path))
    with TestClient(app) as client:
        assert client.patch("/api/server", json=payload).status_code == 422
        assert client.get("/api/server").json()["server_name"] == "original"
    assert path.read_bytes() == original


def test_failed_atomic_save_keeps_original_name_and_cleans_temp_file(tmp_path, monkeypatch):
    path = config_file(tmp_path, 'server_name = "original"\n')
    original = path.read_bytes()
    app = settings_app(load_config(path))
    def denied(*args):
        raise PermissionError("read-only")
    monkeypatch.setattr(os, "replace", denied)
    with TestClient(app) as client:
        assert client.patch("/api/server", json={"server_name": "new"}).status_code == 500
        assert client.get("/api/server").json()["server_name"] == "original"
        assert app.state.node_registry.server_name == "original"
    assert path.read_bytes() == original
    assert not list(path.parent.glob(f".{path.name}.*"))


def test_configuration_error_does_not_overwrite_file(tmp_path):
    path = config_file(tmp_path)
    config = load_config(path)
    path.write_text('invalid = [\n')
    invalid = path.read_bytes()
    app = settings_app(config)
    with TestClient(app) as client:
        assert client.patch("/api/server", json={"server_name": "new"}).status_code == 409
    assert path.read_bytes() == invalid
