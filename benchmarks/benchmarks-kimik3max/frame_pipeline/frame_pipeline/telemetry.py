"""Aggregate host/device telemetry sampler.

NVML is used when available to sample GPU utilization, GPU memory, and the
NVENC/video-engine (encoder) utilization. CPU utilization comes from
``/proc/stat``. All samples are *device-wide* (aggregate), never per-stream:
NVML cannot attribute utilization to a single encoder session, so the recorder
labels this channel ``aggregate`` honestly.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

try:
    import pynvml  # type: ignore

    _NVML_ERR: Optional[Exception] = None
except Exception as exc:  # pragma: no cover - pynvml may be absent
    pynvml = None  # type: ignore
    _NVML_ERR = exc


def _read_proc_stat() -> tuple[int, int]:
    """Return (busy_jiffies, total_jiffies) from /proc/stat CPU line."""

    with open("/proc/stat", "r", encoding="utf-8") as fh:
        parts = fh.readline().split()
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    return total - idle, total


class TelemetrySampler:
    """Background thread sampling aggregate device telemetry at a fixed interval."""

    def __init__(self, interval_s: float = 0.5, gpu_indices: Optional[list[int]] = None) -> None:
        self.interval_s = interval_s
        self.gpu_indices = gpu_indices
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._nvml = False
        self._handles: list = []
        self._cuda_visible_devices: Optional[str] = os.environ.get("CUDA_VISIBLE_DEVICES")

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if pynvml is not None:
            try:
                pynvml.nvmlInit()
                count = pynvml.nvmlDeviceGetCount()
                idxs = self.gpu_indices if self.gpu_indices is not None else list(range(count))
                self._handles = [pynvml.nvmlDeviceGetHandleByIndex(i) for i in idxs]
                self._nvml = True
            except Exception:
                self._nvml = False
        self._prev = _read_proc_stat()
        self._thread = threading.Thread(target=self._run, name="telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._nvml:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    # -- sampling -----------------------------------------------------------
    def _run(self) -> None:
        prev = self._prev
        while not self._stop.is_set():
            t0 = time.monotonic()
            rec: dict[str, Any] = {"t_wall_ns": time.time_ns()}
            busy, total = _read_proc_stat()
            dt = total - prev[1]
            db = busy - prev[0]
            rec["cpu_util_pct"] = round(100.0 * db / dt, 2) if dt > 0 else 0.0
            prev = (busy, total)
            if self._nvml:
                gpus = []
                for i, h in enumerate(self._handles):
                    try:
                        util = pynvml.nvmlDeviceGetUtilizationRates(h)
                        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                        enc_util, _enc_period = pynvml.nvmlDeviceGetEncoderUtilization(h)
                        gpus.append({
                            "index": (self.gpu_indices[i] if self.gpu_indices else i),
                            "gpu_util_pct": util.gpu,
                            "mem_used_mb": round(mem.used / 1e6, 1),
                            "enc_util_pct": enc_util,
                        })
                    except Exception as exc:
                        gpus.append({"index": i, "error": str(exc)})
                rec["gpus"] = gpus
            self.samples.append(rec)
            elapsed = time.monotonic() - t0
            self._stop.wait(max(0.0, self.interval_s - elapsed))

    def snapshot(self) -> dict:
        return {
            "note": "device-wide/aggregate; NVML cannot attribute to a single encoder session",
            "cuda_visible_devices": self._cuda_visible_devices,
            "nvml": self._nvml,
            "sample_count": len(self.samples),
            "samples": self.samples,
        }

    def summarize(self) -> dict:
        """Aggregate the samples into min/max/mean per metric (aggregate channel)."""

        out: dict[str, Any] = {
            "note": "device-wide/aggregate; not per-stream",
            "nvml": self._nvml,
            "sample_count": len(self.samples),
        }
        if not self.samples:
            return out
        cpu = [s.get("cpu_util_pct", 0.0) for s in self.samples]
        out["cpu_util_pct"] = _agg(cpu)
        # collate per-GPU if present
        per_gpu: dict[int, dict[str, list]] = {}
        for s in self.samples:
            for g in s.get("gpus", []):
                if "error" in g:
                    continue
                gi = g["index"]
                per_gpu.setdefault(gi, {}).setdefault("gpu_util_pct", []).append(g["gpu_util_pct"])
                per_gpu[gi].setdefault("enc_util_pct", []).append(g["enc_util_pct"])
                per_gpu[gi].setdefault("mem_used_mb", []).append(g["mem_used_mb"])
        out["gpus"] = {str(k): {m: _agg(v) for m, v in metrics.items()} for k, metrics in per_gpu.items()}
        return out


def _agg(vals: list) -> dict:
    if not vals:
        return {}
    return {"min": round(min(vals), 2), "mean": round(sum(vals) / len(vals), 2), "max": round(max(vals), 2)}


def available() -> bool:
    return pynvml is not None and _NVML_ERR is None
