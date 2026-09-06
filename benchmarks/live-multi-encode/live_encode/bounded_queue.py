"""Bounded real-time queue with a freshness policy.

Adapted from the read-only reference
``benchmarks/benchmarks-kimik3max/.../bounded_queue.py``. Used inside the encoder
orchestration for non-SHM control flow. The SHM signal path itself uses a bounded
``multiprocessing.Queue`` (producer ``put_nowait`` -> drop-on-full -> counter).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class QueueStats:
    capacity: int
    depth: int = 0
    high_water: int = 0
    enqueued: int = 0
    dequeued: int = 0
    dropped: int = 0
    replacements: int = 0
    total_wait_ms: float = 0.0

    def as_dict(self) -> dict:
        mean_wait = self.total_wait_ms / self.dequeued if self.dequeued else 0.0
        return {
            "capacity": self.capacity,
            "high_water": self.high_water,
            "enqueued": self.enqueued,
            "dequeued": self.dequeued,
            "dropped": self.dropped,
            "replacements": self.replacements,
            "mean_queue_wait_ms": round(mean_wait, 4),
        }


class _Item:
    __slots__ = ("value", "enqueue_ns")

    def __init__(self, value: Any, enqueue_ns: int) -> None:
        self.value = value
        self.enqueue_ns = enqueue_ns


class BoundedFrameQueue:
    """Thread-safe bounded queue; policy in {replace_oldest, drop_new, block}."""

    def __init__(self, capacity: int, policy: str = "replace_oldest", name: str = "q"):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if policy not in ("replace_oldest", "drop_new", "block"):
            raise ValueError(f"unknown policy {policy!r}")
        self._cap = capacity
        self._policy = policy
        self.name = name
        self._items: list[_Item] = []
        self._cond = threading.Condition()
        self._closed = False
        self.stats = QueueStats(capacity=capacity)

    def put(self, value: Any) -> bool:
        now = time.monotonic_ns()
        with self._cond:
            if self._closed:
                return False
            if len(self._items) < self._cap:
                self._items.append(_Item(value, now))
                self.stats.enqueued += 1
                self._hw()
                self._cond.notify()
                return True
            if self._policy == "replace_oldest":
                self._items.pop(0)
                self._items.append(_Item(value, now))
                self.stats.replacements += 1
                self.stats.enqueued += 1
                self._hw()
                self._cond.notify()
                return True
            if self._policy == "drop_new":
                self.stats.dropped += 1
                return False
            # block (non-real-time; tests only)
            while len(self._items) >= self._cap and not self._closed:
                self._cond.wait()
            if self._closed:
                return False
            self._items.append(_Item(value, now))
            self.stats.enqueued += 1
            self._hw()
            self._cond.notify()
            return True

    def _hw(self) -> None:
        d = len(self._items)
        self.stats.depth = d
        if d > self.stats.high_water:
            self.stats.high_water = d

    def get(self, timeout: Optional[float] = None) -> Optional[Any]:
        deadline = None if timeout is None else (time.monotonic() + timeout)
        with self._cond:
            while not self._items and not self._closed:
                remaining = None if deadline is None else (deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    return None
                self._cond.wait(remaining)
            if not self._items:
                return None
            item = self._items.pop(0)
            self.stats.dequeued += 1
            self.stats.total_wait_ms += (time.monotonic_ns() - item.enqueue_ns) / 1e6
            self.stats.depth = len(self._items)
            self._cond.notify_all()
            return item.value

    def depth(self) -> int:
        with self._cond:
            return len(self._items)

    def oldest_age_ms(self) -> float:
        with self._cond:
            if not self._items:
                return 0.0
            return (time.perf_counter_ns() - self._items[0].enqueue_ns) / 1e6

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def snapshot(self) -> dict:
        with self._cond:
            return self.stats.as_dict() | {"name": self.name, "policy": self._policy}
