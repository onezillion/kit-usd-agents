"""Telemetry for the KHL Frame Pipeline Benchmark.

Records per-frame events as JSONL (one line per produced/captured/encoded frame),
maintains per-stream counters, and produces summary JSON + a CSV comparison table.

We deliberately record raw events so post-processing can derive p50/p95/p99/max
latency distributions without losing precision.  Aggregate host/device telemetry
(nvidia-smi) is captured as device-wide (labelled `aggregate`), never per-stream.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _percentiles(values: list, pts=(50, 95, 99)) -> dict:
    if not values:
        return {f"p{p}": 0.0 for p in pts} | {"max": 0.0, "mean": 0.0, "n": 0}
    s = sorted(values)
    n = len(s)
    out = {}
    for p in pts:
        # nearest-rank
        idx = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
        out[f"p{p}"] = s[idx]
    out["max"] = s[-1]
    out["mean"] = statistics.fmean(s)
    out["n"] = n
    return out


@dataclass
class StreamCounters:
    produced: int = 0
    encoded: int = 0
    dropped: int = 0
    replaced: int = 0
    # latency seconds (capture-ready -> encoded-ready)
    latencies: list = field(default_factory=list)
    # durations seconds
    capture_cb_durations: list = field(default_factory=list)
    color_convert_durations: list = field(default_factory=list)
    frame_prep_durations: list = field(default_factory=list)
    encoder_submit_durations: list = field(default_factory=list)
    encoder_completion_durations: list = field(default_factory=list)
    # encoded AU sizes (bytes) — keyed frame id / size / is_keyframe
    au_sizes: list = field(default_factory=list)
    keyframe_sizes: list = field(default_factory=list)
    inter_sizes: list = field(default_factory=list)
    # frame interval jitter (seconds between successive encoded AUs)
    intervals: list = field(default_factory=list)
    queue_stats: dict = field(default_factory=dict)
    # copy counters (explicit)
    d2d_copies: int = 0
    d2h_copies: int = 0
    h2d_copies: int = 0

    def add_latency(self, secs: float):
        self.latencies.append(secs)

    def summary(self) -> dict:
        lat_pct = _percentiles(self.latencies)
        return {
            "produced": self.produced,
            "encoded": self.encoded,
            "dropped": self.dropped,
            "replaced": self.replaced,
            "fps_produced": self.produced / (self._span or 1.0),
            "fps_encoded": self.encoded / (self._span or 1.0),
            "latency_s": lat_pct,
            "capture_cb_duration_s": _percentiles(self.capture_cb_durations),
            "color_convert_duration_s": _percentiles(self.color_convert_durations),
            "frame_prep_duration_s": _percentiles(self.frame_prep_durations),
            "encoder_submit_duration_s": _percentiles(self.encoder_submit_durations),
            "encoder_completion_duration_s": _percentiles(self.encoder_completion_durations),
            "frame_interval_jitter_s": _percentiles(self.intervals),
            "au_size_bytes": _percentiles(self.au_sizes),
            "idr_size_bytes": _percentiles(self.keyframe_sizes),
            "inter_size_bytes": _percentiles(self.inter_sizes),
            "queue": self.queue_stats,
            "copies": {"d2d": self.d2d_copies, "d2h": self.d2h_copies, "h2d": self.h2d_copies},
            "_span_s": self._span,
        }

    # internal: span is set by the harness at stream finish
    _span: float = 0.0


class Telemetry:
    """Per-run telemetry writer.

    One JSONL file per stream.  Each event is one JSON line.
    Aggregate telemetry (nvidia-smi) is polled on a background thread and labelled
    `aggregate` — never per-stream.
    """

    def __init__(self, run_dir: str, stream_id: str, enable_nvsmi: bool = True):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.stream_id = stream_id
        self.counters = StreamCounters()
        self._events_path = self.run_dir / f"events_{stream_id}.jsonl"
        self._fh = open(self._events_path, "a", buffering=1)  # line-buffered
        self._lock = threading.Lock()
        self._last_encoded_ts: Optional[float] = None
        self._nvsmi_stop = threading.Event()
        self._nvsmi_thread: Optional[threading.Thread] = None
        self._nvsmi_path = self.run_dir / f"nvsmi_{stream_id}.jsonl"
        self._nvsmi_fh = None
        if enable_nvsmi:
            self._start_nvsmi()

    # ------ event recording -------------------------------------------------
    def event(self, kind: str, payload: dict):
        rec = {"t": time.time(), "stream": self.stream_id, "kind": kind}
        rec.update(payload)
        with self._lock:
            self._fh.write(json.dumps(rec, default=float) + "\n")

    def record_encoded(self, au_summary: dict, capture_ts: float, submit_ts: float,
                       completion_ts: float):
        """Update counters when an EncodedAccessUnit becomes available."""
        with self._lock:
            self.counters.encoded += 1
            self.counters.latencies.append(completion_ts - capture_ts)
            self.counters.encoder_submit_durations.append(submit_ts - capture_ts)
            self.counters.encoder_completion_durations.append(completion_ts - submit_ts)
            sz = au_summary.get("encoded_size_bytes", 0)
            self.counters.au_sizes.append(sz)
            if au_summary.get("is_keyframe"):
                self.counters.keyframe_sizes.append(sz)
            else:
                self.counters.inter_sizes.append(sz)
            if self._last_encoded_ts is not None:
                self.counters.intervals.append(completion_ts - self._last_encoded_ts)
            self._last_encoded_ts = completion_ts
        self.event("encoded", au_summary)

    def record_captured(self, frame_summary: dict, capture_cb_duration: float,
                        color_convert: float, frame_prep: float, copies: dict):
        with self._lock:
            self.counters.produced += 1
            self.counters.capture_cb_durations.append(capture_cb_duration)
            self.counters.color_convert_durations.append(color_convert)
            self.counters.frame_prep_durations.append(frame_prep)
            self.counters.d2d_copies += int(copies.get("d2d", 0))
            self.counters.d2h_copies += int(copies.get("d2h", 0))
            self.counters.h2d_copies += int(copies.get("h2d", 0))
        self.event("captured", frame_summary)

    def record_queue_stats(self, qstats: dict):
        with self._lock:
            self.counters.queue_stats = qstats
            self.counters.dropped = qstats.get("dropped", 0)
            self.counters.replaced = qstats.get("replaced", 0)
        self.event("queue", qstats)

    def set_span(self, span_s: float):
        self.counters._span = span_s

    # ------ aggregate telemetry (nvidia-smi, device-wide) -------------------
    def _start_nvsmi(self):
        try:
            self._nvsmi_fh = open(self._nvsmi_path, "a", buffering=1)
        except Exception:
            return
        t = threading.Thread(target=self._nvsmi_loop, name=f"nvsmi-{self.stream_id}",
                             daemon=True)
        t.start()
        self._nvsmi_thread = t

    def _nvsmi_loop(self):
        cmd = ["nvidia-smi",
               "--query-gpu=index,timestamp,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw",
               "--format=csv,noheader,nounits",
               "--loop-ms=500"]
        while not self._nvsmi_stop.is_set():
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
                if proc.returncode == 0:
                    for line in proc.stdout.strip().splitlines():
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 7:
                            rec = {"t": time.time(), "scope": "aggregate", "gpu_index": parts[0],
                                   "gpu_util_pct": float(parts[2]) if parts[2] != "" else None,
                                   "mem_util_pct": float(parts[3]) if parts[3] != "" else None,
                                   "mem_used_mib": float(parts[4]) if parts[4] != "" else None,
                                   "mem_total_mib": float(parts[5]) if parts[5] != "" else None,
                                   "power_w": float(parts[6]) if parts[6] != "" else None}
                            # NVENC/video engine util is not exposed via --query-gpu; we record
                            # what nvidia-smi actually provides and LABEL it aggregate.
                            self._nvsmi_fh.write(json.dumps(rec, default=float) + "\n")
            except Exception:
                pass
            self._nvsmi_stop.wait(0.5)

    def close(self):
        self._nvsmi_stop.set()
        if self._nvsmi_thread:
            self._nvsmi_thread.join(timeout=2.0)
        try:
            if self._nvsmi_fh:
                self._nvsmi_fh.flush(); self._nvsmi_fh.close()
        except Exception:
            pass
        with self._lock:
            try:
                self._fh.flush(); self._fh.close()
            except Exception:
                pass

    def flush(self):
        with self._lock:
            try:
                self._fh.flush()
            except Exception:
                pass
