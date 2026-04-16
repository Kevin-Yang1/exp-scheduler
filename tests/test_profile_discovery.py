from __future__ import annotations

import json
from pathlib import Path

from exp_scheduler_app.profile_discovery import (
    discover_conda_environments,
    discover_venvs,
)


class FakeCompletedProcess:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_discover_conda_environments_parses_info(monkeypatch, tmp_path):
    conda_root = tmp_path / "miniconda3"
    conda_sh = conda_root / "etc" / "profile.d" / "conda.sh"
    conda_sh.parent.mkdir(parents=True)
    conda_sh.write_text("# conda\n", encoding="utf-8")
    conda_bin = conda_root / "bin" / "conda"
    conda_bin.parent.mkdir(parents=True)
    conda_bin.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "exp_scheduler_app.profile_discovery.find_conda_executable",
        lambda: conda_bin,
    )

    def fake_runner(command):
        assert command == [str(conda_bin), "info", "--json"]
        payload = {
            "root_prefix": str(conda_root),
            "envs": [str(conda_root), str(conda_root / "envs" / "demo")],
            "envs_details": {
                str(conda_root): {"name": "base"},
                str(conda_root / "envs" / "demo"): {"name": "demo"},
            },
        }
        return FakeCompletedProcess(stdout=json.dumps(payload))

    candidates, executable = discover_conda_environments(command_runner=fake_runner)
    assert executable == conda_bin
    assert [item["display_name"] for item in candidates] == ["base", "demo"]
    assert "conda activate base" in candidates[0]["suggested_profile"]["shell_setup"]
    assert "conda activate demo" in candidates[1]["suggested_profile"]["shell_setup"]


def test_discover_venvs_finds_common_directories(tmp_path):
    project_a = tmp_path / "project-a"
    project_a_venv = project_a / ".venv" / "bin"
    project_a_venv.mkdir(parents=True)
    (project_a_venv / "activate").write_text("", encoding="utf-8")
    (project_a_venv / "python").write_text("", encoding="utf-8")

    project_b = tmp_path / "project-b"
    project_b_venv = project_b / "venv" / "bin"
    project_b_venv.mkdir(parents=True)
    (project_b_venv / "activate").write_text("", encoding="utf-8")
    (project_b_venv / "python").write_text("", encoding="utf-8")

    candidates = discover_venvs(search_roots=[tmp_path], max_depth=3)
    paths = [item["path"] for item in candidates]
    assert str(project_a / ".venv") in paths
    assert str(project_b / "venv") in paths
    assert any(item["suggested_profile"]["name"] == "venv:project-a" for item in candidates)
