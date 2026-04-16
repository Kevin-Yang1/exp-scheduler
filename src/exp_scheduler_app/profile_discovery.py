from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path
import shutil
import subprocess


COMMON_VENV_DIR_NAMES = frozenset({".venv", "venv", ".env", "env"})
DEFAULT_SKIP_DIR_NAMES = frozenset(
    {
        ".cache",
        ".git",
        ".hg",
        ".idea",
        ".local",
        ".mypy_cache",
        ".next",
        ".npm",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "anaconda3",
        "build",
        "dist",
        "miniconda3",
        "miniforge3",
        "mambaforge",
        "node_modules",
    }
)
MAX_VENV_SCAN_DEPTH = 4
MAX_DISCOVERY_RESULTS = 200


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def default_command_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def discover_installed_environments(
    *,
    search_roots: Sequence[Path] | None = None,
    command_runner: CommandRunner | None = None,
) -> dict[str, object]:
    roots = resolve_search_roots(search_roots)
    runner = command_runner or default_command_runner
    conda_envs, conda_executable = discover_conda_environments(command_runner=runner)
    venvs = discover_venvs(search_roots=roots)
    return {
        "conda_envs": conda_envs,
        "venvs": venvs,
        "search_roots": [str(root) for root in roots],
        "conda_executable": str(conda_executable) if conda_executable else None,
    }


def resolve_search_roots(search_roots: Sequence[Path] | None = None) -> list[Path]:
    if search_roots is None:
        cwd = Path.cwd().resolve()
        home = Path.home().resolve()
        candidates = [cwd, cwd.parent, home]
        for suffix in ("projects", "code", "work"):
            maybe_dir = home / suffix
            if maybe_dir.is_dir():
                candidates.append(maybe_dir.resolve())
    else:
        candidates = [Path(root).expanduser().resolve() for root in search_roots]

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_dir():
            continue
        if candidate in seen:
            continue
        roots.append(candidate)
        seen.add(candidate)
    return roots


def discover_conda_environments(
    *,
    command_runner: CommandRunner | None = None,
) -> tuple[list[dict[str, object]], Path | None]:
    runner = command_runner or default_command_runner
    conda_executable = find_conda_executable()
    if conda_executable is None:
        return [], None

    result = runner([str(conda_executable), "info", "--json"])
    if result.returncode != 0:
        return [], conda_executable

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return [], conda_executable
    root_prefix = Path(
        payload.get("root_prefix") or payload.get("conda_prefix") or conda_executable.parent.parent
    ).resolve()
    env_paths = [Path(item).resolve() for item in payload.get("envs", [])]
    envs_details = payload.get("envs_details") or {}
    conda_sh = root_prefix / "etc" / "profile.d" / "conda.sh"
    candidates: list[dict[str, object]] = []

    for env_path in env_paths[:MAX_DISCOVERY_RESULTS]:
        details = envs_details.get(str(env_path), {})
        env_name = details.get("name") or infer_conda_name(root_prefix, env_path)
        activation_target = env_name if env_name else str(env_path)
        shell_setup_lines = []
        if conda_sh.exists():
            shell_setup_lines.append(f"source {conda_sh}")
        shell_setup_lines.append(f"conda activate {activation_target}")
        candidates.append(
            {
                "id": f"conda::{env_path}",
                "kind": "conda",
                "display_name": env_name or env_path.name or str(env_path),
                "path": str(env_path),
                "python_path": str(env_path / "bin" / "python"),
                "suggested_profile": {
                    "name": f"conda:{env_name or env_path.name or 'env'}",
                    "cwd": None,
                    "env": {},
                    "shell_setup": "\n".join(shell_setup_lines),
                    "notes": f"Auto imported from conda environment at {env_path}",
                },
            }
        )

    apply_unique_profile_names(candidates)
    return candidates, conda_executable


def find_conda_executable() -> Path | None:
    found = shutil.which("conda")
    if found:
        return Path(found).resolve()

    home = Path.home()
    common_candidates = [
        home / "miniconda3" / "bin" / "conda",
        home / "anaconda3" / "bin" / "conda",
        home / "mambaforge" / "bin" / "conda",
        home / "miniforge3" / "bin" / "conda",
    ]
    for candidate in common_candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def infer_conda_name(root_prefix: Path, env_path: Path) -> str | None:
    if env_path == root_prefix:
        return "base"
    envs_dir = root_prefix / "envs"
    try:
        relative = env_path.relative_to(envs_dir)
    except ValueError:
        return env_path.name or None
    if len(relative.parts) == 1:
        return relative.parts[0]
    return env_path.name or None


def discover_venvs(
    *,
    search_roots: Sequence[Path] | None = None,
    max_depth: int = MAX_VENV_SCAN_DEPTH,
) -> list[dict[str, object]]:
    roots = resolve_search_roots(search_roots)
    discovered_paths: list[Path] = []
    seen: set[Path] = set()

    for root in roots:
        if is_venv_directory(root):
            resolved = root.resolve()
            if resolved not in seen:
                discovered_paths.append(resolved)
                seen.add(resolved)
            continue

        base_depth = len(root.parts)
        for current_root, dir_names, _ in os.walk(root, followlinks=False):
            current_path = Path(current_root)
            current_depth = len(current_path.parts) - base_depth

            if current_depth >= max_depth:
                dir_names[:] = []
                continue

            filtered_dir_names: list[str] = []
            for directory_name in dir_names:
                if directory_name in DEFAULT_SKIP_DIR_NAMES:
                    continue
                candidate_path = current_path / directory_name
                if directory_name in COMMON_VENV_DIR_NAMES and is_venv_directory(candidate_path):
                    resolved = candidate_path.resolve()
                    if resolved not in seen:
                        discovered_paths.append(resolved)
                        seen.add(resolved)
                    continue
                filtered_dir_names.append(directory_name)
            dir_names[:] = filtered_dir_names

            if len(discovered_paths) >= MAX_DISCOVERY_RESULTS:
                break

    candidates: list[dict[str, object]] = []
    for venv_path in sorted(discovered_paths)[:MAX_DISCOVERY_RESULTS]:
        project_dir = venv_path.parent
        project_name = project_dir.name or venv_path.name or "project"
        candidates.append(
            {
                "id": f"venv::{venv_path}",
                "kind": "venv",
                "display_name": project_name,
                "path": str(venv_path),
                "python_path": str(venv_path / "bin" / "python"),
                "suggested_profile": {
                    "name": f"venv:{project_name}",
                    "cwd": str(project_dir),
                    "env": {},
                    "shell_setup": f"source {venv_path / 'bin' / 'activate'}",
                    "notes": f"Auto imported from virtualenv at {venv_path}",
                },
            }
        )

    apply_unique_profile_names(candidates)
    return candidates


def is_venv_directory(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "bin" / "activate").is_file()
        and (path / "bin" / "python").exists()
    )


def apply_unique_profile_names(candidates: list[dict[str, object]]) -> None:
    used_names: dict[str, int] = {}
    for candidate in candidates:
        suggested = candidate.get("suggested_profile") or {}
        base_name = str(suggested.get("name") or "profile")
        count = used_names.get(base_name, 0) + 1
        used_names[base_name] = count
        if count == 1:
            continue
        suggested["name"] = f"{base_name}-{count}"
