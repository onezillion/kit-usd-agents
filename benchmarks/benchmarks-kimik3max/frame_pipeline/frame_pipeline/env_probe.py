"""Environment / capability capture.

Records GPU, driver, CUDA, Python, and the encode libraries actually available,
without importing heavy backends at module import time. Used by the harness to
stamp every run with an environment block and to capability-gate backends.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from typing import Any


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout + out.stderr).strip()
    except Exception as exc:  # pragma: no cover
        return f"error: {exc}"


def gpu_info() -> dict:
    if not shutil.which("nvidia-smi"):
        return {"present": False}
    out = _run([
        "nvidia-smi",
        "--query-gpu=index,name,driver_version,memory.total,compute_cap",
        "--format=csv,noheader",
    ])
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            gpus.append({
                "index": parts[0], "name": parts[1], "driver": parts[2],
                "memory_total": parts[3], "compute_cap": parts[4],
            })
    return {"present": bool(gpus), "gpus": gpus}


def library_versions() -> dict:
    """Best-effort versions for the encode libraries relevant to Stage 1."""

    def _v(modname: str, attr: str = "__version__") -> dict:
        try:
            mod = __import__(modname)
            return {"present": True, "version": str(getattr(mod, attr, "unknown"))}
        except Exception as exc:
            return {"present": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "numpy": _v("numpy"),
        "av": _v("av"),
        "PyNvVideoCodec": _v("PyNvVideoCodec"),
        "pynvml": _v("pynvml"),
        "cuda-bindings": _v("cuda.bindings"),
    }


def encoder_caps() -> dict:
    """Query hardware NVENC capabilities (via PyNvVideoCodec if present)."""

    try:
        import PyNvVideoCodec as nvc  # type: ignore

        caps = nvc.GetEncoderCaps(0, "h264")
        keep = {
            "num_encoder_engines", "mb_per_sec_max", "async_encode_support",
            "width_max", "height_max", "width_min", "height_min",
            "num_max_bframes", "support_lookahead", "support_yuv444_encode",
            "supportedNvEncVersion",
        }
        return {k: v for k, v in caps.items() if k in keep} | {
            "supportedNvEncVersion": getattr(nvc, "supportedNvEncVersion", None)
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def environment() -> dict:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
        },
        "gpu": gpu_info(),
        "libraries": library_versions(),
        "nvenc_encoder_caps": encoder_caps(),
        "ffmpeg": _run(["ffmpeg", "-hide_banner", "-version"]).splitlines()[:1] if shutil.which("ffmpeg") else None,
    }
