"""Bounded freshness-preferring queue for real-time frame pipelining.

Policy (per benchmark spec): queues are bounded and PREFER FRESHNESS over accumulating
latency.  On overflow by a producer we DROP the OLDEST in-flight frame and replace it with
the newest (single-slot freshness semantics) — except when an explicit "drop" policy is
requested.  This module separates application queueing/scheduling from hardware encode
latency so the report never hides queueing latency inside encoder latency.

Thread-safe.  Designed for one producer thread (capture/source) and one consumer thread
(encoder worker) per stream.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class QueueStats:
    depth: int = 0
    high_water: int = 0
    dropped: int = 0          # frames dropped because queue full and policy=drop
    replaced: int = 0         # frames replaced (oldest ousted) under policy=replace_oldest
    # rolling age of the oldest in-queue frame, seconds (sampled at each put/get)
    oldest_age_samples: list = field(default_factory=list)

    def as_dict(self) -> dict:
        import statistics
        oldest = self.oldest_age_samples
        return {
            "depth": self.depth,
            "high_water": self.high_water,
            "dropped": self.dropped,
            "replaced": self.replaced,
            "oldest_age_p50": statistics.median(oldest) if oldest else 0.0,
            "oldest_age_max": max(oldest) if oldest else 0.0,
            "oldest_age_samples": len(oldest),
        }


class BoundedFrameQueue:
    """A bounded queue of CapturedFrame-like items with freshness policy.

    policy:
      "replace_oldest" (default) — on put when full: drop the oldest, append newest, count replaced.
      "drop_newest"               — on put when full: drop the incoming frame, count dropped.
    """

    def __init__(self, maxsize: int, policy: str = "replace_oldest",
                 clock: Callable[[], float] = time.perf_counter):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        if policy not in ("replace_oldest", "drop_newest"):
            raise ValueError("unknown policy: %r" % policy)
        self._max = maxsize
        self._policy = policy
        self._clock = clock
        self._dq: deque = deque()
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self.stats = QueueStats()

    # ----- producer side -----------------------------------------------------
    def put(self, frame: Any, block: bool = False, timeout: Optional[float] = None) -> bool:
        """Try to enqueue a frame.  Returns True if accepted, False if dropped.

        frame must expose `.capture_ts` (float) for age stats and `.frame_id` (int).
        """
        deadline = None
        if block and timeout is not None:
            deadline = self._clock() + timeout
        with self._not_empty:
            now = self._clock()
            if len(self._dq) >= self._max:
                if self._policy == "drop_newest":
                    self.stats.dropped += 1
                    return False
                # replace_oldest: evict the oldest, record its age
                evicted = self._dq.popleft()
                self.stats.replaced += 1
                self.stats.oldest_age_samples.append(now - getattr(evicted, "capture_ts", now))
                # truncate rolling samples to keep memory bounded
                if len(self.stats.oldest_age_samples) > 4096:
                    self.stats.oldest_age_samples = self.stats.oldest_age_samples[-2048:]
            self._dq.append(frame)
            if len(self._dq) > self.stats.high_water:
                self.stats.high_water = len(self._dq)
            self.stats.depth = len(self._dq)
            # sample oldest age if there was already an older frame
            if len(self._dq) > 1:
                oldest = self._dq[0]
                self.stats.oldest_age_samples.append(now - getattr(oldest, "capture_ts", now))
                if len(self.stats.oldest_age_samples) > 4096:
                    self.stats.oldest_age_samples = self.stats.oldest_age_samples[-2048:]
            self._not_empty.notify()
            return True

    # ----- consumer side -----------------------------------------------------
    def get(self, block: bool = True, timeout: Optional[float] = None) -> Optional[Any]:
        with self._not_empty:
            if not block and not self._dq:
                return None
            if block:
                end = None if timeout is None else self._clock() + timeout
                while not self._dq:
                    if end is None:
                        self._not_empty.wait()
                    else:
                        remaining = end - self._clock()
                        if remaining <= 0:
                            return None
                        self._not_empty.wait(timeout=remaining)
            frame = self._dq.popleft()
            now = self._clock()
            self.stats.oldest_age_samples.append(now - getattr(frame, "capture_ts", now))
            if len(self.stats.oldest_age_samples) > 4096:
                self.stats.oldest_age_samples = self.stats.oldest_age_samples[-2048:]
            self.stats.depth = len(self._dq)
            return frame

    def qsize(self) -> int:
        with self._lock:
            return len(self._dq)

    def stats_snapshot(self) -> dict:
        with self._lock:
            return self.stats.as_dict()

    def drain(self) -> list:
        """Drain all remaining frames (used at shutdown)."""
        with self._lock:
            out = list(self._dq)
            self._dq.clear()
            self.stats.depth = 0
            return out
