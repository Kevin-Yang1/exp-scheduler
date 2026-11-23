from fastapi.testclient import TestClient
import pytest

from exp_scheduler_app.config import SchedulerConfig
from exp_scheduler_app.system_metrics import SystemMetrics
from exp_scheduler_app.web import create_app


@pytest.fixture
def order_api(tmp_path, monkeypatch):
    payload = {"sampled_at": "cached", "disks": [
        {"mountpoint": "/", "device_id": "8:1"},
        {"mountpoint": "/data", "device_id": "8:2"},
        {"mountpoint": "/boot/efi", "device_id": "8:3"},
    ]}
    monkeypatch.setattr(SystemMetrics, "snapshot", lambda self: payload)
    config = SchedulerConfig(state_dir=tmp_path, log_dir=tmp_path / "logs")
    def build():
        def no_gpu_queries():
            raise AssertionError("Disk ordering must not query GPUs or scheduling")
        app = create_app(config, gpu_provider=no_gpu_queries, autostart=False)
        app.state.scheduler.database.init()
        return app
    return build, payload


def test_saved_order_is_shared_persistent_and_does_not_change_cached_metrics(order_api):
    build, cached = order_api
    with TestClient(build()) as client:
        assert client.get("/api/system/disks/order").json() == {"mountpoints": []}
        response = client.put("/api/system/disks/order", json={"mountpoints": ["/data", "/boot/efi", "/"]})
        assert response.status_code == 200
        assert response.json() == {"mountpoints": ["/data", "/boot/efi", "/"]}
        metrics = client.get("/api/system/metrics").json()
        assert [disk["mountpoint"] for disk in metrics["disks"]] == ["/data", "/boot/efi", "/"]
        assert metrics["disk_order"] == ["/data", "/boot/efi", "/"]
        assert metrics["sampled_at"] == "cached"
        assert [disk["mountpoint"] for disk in cached["disks"]] == ["/", "/data", "/boot/efi"]
    with TestClient(build()) as reopened:
        assert reopened.get("/api/system/disks/order").json()["mountpoints"] == ["/data", "/boot/efi", "/"]
        assert reopened.get("/api/system/metrics").json()["disks"][0]["mountpoint"] == "/data"


def test_missing_disks_keep_their_place_and_new_disks_follow_existing_order(order_api):
    build, cached = order_api
    with TestClient(build()) as client:
        expected = ["/data", "/boot/efi", "/"]
        client.put("/api/system/disks/order", json={"mountpoints": expected})
        cached["disks"] = [{"mountpoint": "/", "device_id": "8:1"}, {"mountpoint": "/new", "device_id": "8:4"}]
        assert [disk["mountpoint"] for disk in client.get("/api/system/metrics").json()["disks"]] == ["/", "/new"]
        assert client.get("/api/system/disks/order").json()["mountpoints"] == expected
        cached["disks"].append({"mountpoint": "/data", "device_id": "259:42"})
        assert [disk["mountpoint"] for disk in client.get("/api/system/metrics").json()["disks"]] == ["/data", "/", "/new"]
        client.put("/api/system/disks/order", json={"mountpoints": []})
        assert client.get("/api/system/metrics").json()["disks"] == cached["disks"]


@pytest.mark.parametrize("payload", [
    {}, {"mountpoints": None}, {"mountpoints": "/data"}, {"mountpoints": [1]},
    {"mountpoints": ["relative"]}, {"mountpoints": [""]},
    {"mountpoints": ["/data", "/data"]}, {"mountpoints": ["/bad\x00path"]},
    {"mountpoints": ["/" + "a" * 4096]}, {"mountpoints": [f"/{i}" for i in range(1025)]},
    {"mountpoints": ["/data"], "extra": True},
])
def test_invalid_order_never_overwrites_saved_preferences(order_api, payload):
    build, _ = order_api
    with TestClient(build()) as client:
        client.put("/api/system/disks/order", json={"mountpoints": ["/data", "/"]})
        assert client.put("/api/system/disks/order", json=payload).status_code == 422
        assert client.get("/api/system/disks/order").json() == {"mountpoints": ["/data", "/"]}


def test_mountpoints_with_spaces_and_unicode_can_be_saved(order_api):
    build, _ = order_api
    with TestClient(build()) as client:
        order = ["/data disk", "/实验", "/"]
        assert client.put("/api/system/disks/order", json={"mountpoints": order}).json() == {"mountpoints": order}
        assert client.get("/api/system/disks/order").json() == {"mountpoints": order}
