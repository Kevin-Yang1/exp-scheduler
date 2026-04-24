from __future__ import annotations

from dataclasses import dataclass
import subprocess


GPU_QUERY = (
    "index,uuid,name,memory.total,memory.used,utilization.gpu"
)
GPU_PROCESS_QUERY = "gpu_uuid,pid,used_memory"


@dataclass(slots=True)
class GPUInfo:
    index: int
    uuid: str
    name: str
    memory_total_mb: int
    memory_used_mb: int
    utilization_gpu: int
    has_processes: bool = False

    def to_dict(
        self,
        *,
        threshold_mb: int,
        scheduler_occupied: bool,
        globally_enabled: bool,
    ) -> dict[str, object]:
        is_idle = (
            self.memory_used_mb < threshold_mb
            and not self.has_processes
            and not scheduler_occupied
            and globally_enabled
        )
        free_memory_mb = max(0, self.memory_total_mb - self.memory_used_mb)
        return {
            "index": self.index,
            "uuid": self.uuid,
            "name": self.name,
            "memory_total_mb": self.memory_total_mb,
            "memory_used_mb": self.memory_used_mb,
            "memory_free_mb": free_memory_mb,
            "utilization_gpu": self.utilization_gpu,
            "has_processes": self.has_processes,
            "scheduler_occupied": scheduler_occupied,
            "globally_enabled": globally_enabled,
            "is_idle": is_idle,
        }


def _run_nvidia_smi(query: str, *, process_query: bool = False) -> subprocess.CompletedProcess[str]:
    command = [
        "nvidia-smi",
        f"--query-{'compute-apps' if process_query else 'gpu'}={query}",
        "--format=csv,noheader,nounits",
    ]
    return subprocess.run(command, capture_output=True, text=True, check=False)


def query_gpus() -> list[GPUInfo]:
    result = _run_nvidia_smi(GPU_QUERY)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "nvidia-smi 调用失败")

    gpus: list[GPUInfo] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [item.strip() for item in line.split(",")]
        if len(parts) != 6:
            continue
        gpus.append(
            GPUInfo(
                index=int(parts[0]),
                uuid=parts[1],
                name=parts[2],
                memory_total_mb=int(parts[3]),
                memory_used_mb=int(parts[4]),
                utilization_gpu=int(parts[5]),
            )
        )

    process_result = _run_nvidia_smi(GPU_PROCESS_QUERY, process_query=True)
    process_uuids: set[str] = set()
    if process_result.returncode == 0:
        for raw_line in process_result.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.lower().startswith("no running processes found"):
                continue
            parts = [item.strip() for item in line.split(",")]
            if len(parts) >= 1:
                process_uuids.add(parts[0])
    else:
        combined = f"{process_result.stdout}\n{process_result.stderr}".lower()
        if "no running processes found" not in combined:
            raise RuntimeError(
                process_result.stderr.strip()
                or process_result.stdout.strip()
                or "nvidia-smi 进程查询失败"
            )

    for gpu in gpus:
        gpu.has_processes = gpu.uuid in process_uuids
    return gpus
