from __future__ import annotations

import argparse
import os
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CUDA smoke test for exp-scheduler.")
    parser.add_argument("--seconds", type=float, default=float(os.getenv("GPU_SMOKE_SECONDS", "60")))
    parser.add_argument("--size", type=int, default=int(os.getenv("GPU_SMOKE_SIZE", "4096")))
    parser.add_argument("--sleep", type=float, default=float(os.getenv("GPU_SMOKE_SLEEP", "0.2")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        import torch
    except ImportError:
        print("PyTorch is not installed in this environment.", flush=True)
        return 2

    if not torch.cuda.is_available():
        print("CUDA is not available. Check the selected environment and GPU driver.", flush=True)
        return 2

    device = torch.device("cuda:0")
    visible_devices = os.getenv("CUDA_VISIBLE_DEVICES", "(not set)")
    print(f"CUDA_VISIBLE_DEVICES={visible_devices}", flush=True)
    print(f"torch device={device}", flush=True)
    print(f"gpu name={torch.cuda.get_device_name(device)}", flush=True)
    print(f"matrix size={args.size}x{args.size}", flush=True)
    print(f"duration={args.seconds:.1f}s", flush=True)

    a = torch.randn((args.size, args.size), device=device, dtype=torch.float16)
    b = torch.randn((args.size, args.size), device=device, dtype=torch.float16)
    c = torch.empty((args.size, args.size), device=device, dtype=torch.float16)
    torch.cuda.synchronize()

    started = time.monotonic()
    iteration = 0
    while True:
        iteration += 1
        c = torch.matmul(a, b, out=c)
        if iteration % 5 == 0:
            torch.cuda.synchronize()
            elapsed = time.monotonic() - started
            allocated_gb = torch.cuda.memory_allocated(device) / 1024**3
            reserved_gb = torch.cuda.memory_reserved(device) / 1024**3
            print(
                f"iter={iteration:05d} elapsed={elapsed:6.1f}s "
                f"allocated={allocated_gb:.2f}GB reserved={reserved_gb:.2f}GB "
                f"checksum={float(c[0, 0]):.4f}",
                flush=True,
            )
            if elapsed >= args.seconds:
                break
            time.sleep(args.sleep)

    print("GPU smoke test finished successfully.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
