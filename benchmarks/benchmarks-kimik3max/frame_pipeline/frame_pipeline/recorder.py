"""Metrics recorder: JSONL event log, per-stream latency stats, run summary.

Events are appended as newline-delimited JSON so a crash mid-run still leaves a
parseable record. A ``RunRecorder`` owns one JSONL file plus in-memory
aggregations used to build the machine-readable ``summary.json``.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def percentile(sorted_vals: list, p: float) -> float:
    """Nearest-rank percentile on an already-sorted list. p in [0,100]."""

    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))


@dataclass
class LatencyStats:
    """Accumulates nanosecond latencies; reports p50/p95/p99/max/mean in ms."""

    _vals: list = field(default_factory=list, repr=False)

    def add(self, ns: int | float) -> None:
        self._vals.append(float(ns))

    def summarize_ms(self) -> dict:
        if not self._vals:
            return {"count": 0}
        s = sorted(self._vals)
        n = len(s)
        return {
            "count": n,
            "mean_ms": round(sum(s) / n / 1e6, 4),
            "p50_ms": round(percentile(s, 50) / 1e6, 4),
            "p95_ms": round(percentile(s, 95) / 1e6, 4),
            "p99_ms": round(percentile(s, 99) / 1e6, 4),
            "max_ms": round(s[-1] / 1e6, 4),
            "min_ms": round(s[0] / 1e6, 4),
        }


@dataclass
class Counter:
    name: str
    value: int = 0

    def incr(self, n: int = 1) -> None:
        self.value += n


class RunRecorder:
    """Owns the JSONL event log and per-stream aggregates for one run."""

    def __init__(self, run_dir: str, run_name: str, config: dict) -> None:
        self.run_dir = run_dir
        self.run_name = run_name
        os.makedirs(run_dir, exist_ok=True)
        self.events_path = os.path.join(run_dir, f"{run_name}.events.jsonl")
        self._lock = threading.Lock()
        self._fh = open(self.events_path, "a", encoding="utf-8")
        self.started_wall_ns = time.time_ns()
        self.started_mono_ns = time.monotonic_ns()
        self.config = config
        # per-stream collections
        self.counters: dict[str, Counter] = {}
        self.latencies: dict[str, LatencyStats] = {}
        self.frame_intervals: dict[str, LatencyStats] = {}
        self.encoded_sizes: dict[str, list] = {}
        self.extra: dict[str, Any] = {}
        self.event("run_start", {"config": config})

    # -- primitives ---------------------------------------------------------
    def event(self, kind: str, payload: dict) -> None:
        rec = {"t_wall_ns": time.time_ns(), "t_mono_ns": time.monotonic_ns(), "kind": kind}
        rec.update(payload)
        line = json.dumps(rec, default=str)
        with self._lock:
            self._fh.write(line + "\n")

    def counter(self, stream: str, name: str) -> Counter:
        key = f"{stream}/{name}"
        if key not in self.counters:
            self.counters[key] = Counter(key)
        return self.counters[key]

    def latency(self, stream: str, name: str) -> LatencyStats:
        key = f"{stream}/{name}"
        if key not in self.latencies:
            self.latencies[key] = LatencyStats()
        return self.latencies[key]

    def record_frame_interval(self, stream: str, dt_ns: float) -> None:
        if stream not in self.frame_intervals:
            self.frame_intervals[stream] = LatencyStats()
        self.frame_intervals[stream].add(dt_ns)

    def record_encoded_size(self, stream: str, n: int) -> None:
        self.encoded_sizes.setdefault(stream, []).append(int(n))

    # -- output -------------------------------------------------------------
    def flush(self) -> None:
        with self._lock:
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        self.event("run_end", {"duration_s": round((time.monotonic_ns() - self.started_mono_ns) / 1e9, 4)})
        self.flush()
        with self._lock:
            self._fh.close()

    def build_summary(self) -> dict:
        """Build the machine-readable summary structure (not yet written)."""

        streams: dict[str, Any] = {}
        for key, lat in self.latencies.items():
            stream, name = key.split("/", 1)
            streams.setdefault(stream, {}).setdefault("latency", {})[name] = lat.summarize_ms()
        for key, c in self.counters.items():
            stream, name = key.split("/", 1)
            streams.setdefault(stream, {}).setdefault("counters", {})[name] = c.value
        for stream, lat in self.frame_intervals.items():
            s = sorted(lat._vals)
            if s:
                mean = sum(s) / len(s)
                var = sum((v - mean) ** 2 for v in s) / len(s)
                streams.setdefault(stream, {})["frame_interval"] = {
                    "mean_ms": round(mean / 1e6, 4),
                    "jitter_stddev_ms": round(math.sqrt(var) / 1e6, 4),
                    "count": len(s),
                }
        for stream, sizes in self.encoded_sizes.items():
            if sizes:
                streams.setdefault(stream, {})["encoded"] = {
                    "frame_count": len(sizes),
                    "total_bytes": sum(sizes),
                    "mean_frame_bytes": round(sum(sizes) / len(sizes), 1),
                    "max_frame_bytes": max(sizes),
                }
        return {
            "run_name": self.run_name,
            "config": self.config,
            "duration_s": round((time.monotonic_ns() - self.started_mono_ns) / 1e9, 4),
            "streams": streams,
            "extra": self.extra,
        }

    def write_summary(self) -> str:
        summary = self.build_summary()
        path = os.path.join(self.run_dir, f"{self.run_name}.summary.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)
        return path
