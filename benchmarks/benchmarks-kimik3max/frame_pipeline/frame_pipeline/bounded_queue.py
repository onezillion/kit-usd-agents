"""Bounded real-time queue with a freshness policy.

All real-time queues in the benchmark are bounded. The default policy prefers
*freshness* over accumulating latency: when the queue is full a new frame
*replaces* the oldest pending frame rather than waiting. Frame ages and drops
are recorded so queueing latency is never silently folded into encoder latency.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class QueueStats:
    capacity: int
    depth: int = 0
    high_water: int = 0
    enqueued: int = 0
    dequeued: int = 0
    dropped: int = 0        # producer had to drop the *new* item (policy="drop_new")
    replacements: int = 0   # oldest pending item evicted for a fresher one (policy="replace_oldest")
    oldest_frame_age_ms: float = 0.0
    total_wait_ms: float = 0.0  # sum of (dequeue_ts - enqueue_ts) across items

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
    """Thread-safe bounded queue with explicit freshness policy.

    policy:
      * ``"replace_oldest"`` (default) - full queue evicts the oldest pending
        frame to make room for the fresher one. Counted as a ``replacement``.
      * ``"drop_new"``                 - full queue causes the newest frame to
        be discarded. Counted as a ``drop``.
      * ``"block"``                    - producer blocks until space (NOT real
        time; only for tests).
    """

    def __init__(self, capacity: int, policy: str = "replace_oldest", name: str = "q") -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if policy not in ("replace_oldest", "drop_new", "block"):
            raise ValueError(f"unknown policy {policy!r}")
        self._cap = capacity
        self._policy = policy
        self.name = name
        self._items: "list[_Item]" = []
        self._cond = threading.Condition()
        self._closed = False
        self.stats = QueueStats(capacity=capacity)

    @property
    def is_closed(self) -> bool:
        with self._cond:
            return self._closed

    # -- producer ----------------------------------------------------------
    def put(self, value: Any, block: bool = False, timeout: Optional[float] = None) -> bool:
        now = time.monotonic_ns()
        with self._cond:
            if self._closed:
                return False
            if len(self._items) < self._cap:
                self._items.append(_Item(value, now))
                self.stats.enqueued += 1
                self._update_hw_locked()
                self._cond.notify()
                return True
            # full
            if self._policy == "replace_oldest":
                _evicted = self._items.pop(0)
                self._items.append(_Item(value, now))
                self.stats.replacements += 1
                self.stats.enqueued += 1
                self._update_hw_locked()
                self._cond.notify()
                return True
            if self._policy == "drop_new" and not block:
                self.stats.dropped += 1
                return False
            # block policy
            deadline = None if timeout is None else (time.monotonic() + timeout)
            while len(self._items) >= self._cap and not self._closed:
                remaining = None if deadline is None else (deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    self.stats.dropped += 1
                    return False
                self._cond.wait(remaining)
            if self._closed:
                return False
            self._items.append(_Item(value, now))
            self.stats.enqueued += 1
            self._update_hw_locked()
            self._cond.notify()
            return True

    def _update_hw_locked(self) -> None:
        d = len(self._items)
        self.stats.depth = d
        if d > self.stats.high_water:
            self.stats.high_water = d

    # -- consumer ----------------------------------------------------------
    def get(self, timeout: Optional[float] = None, now_ns_getter=None) -> Optional[Any]:
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
            now = (now_ns_getter() if now_ns_getter else time.monotonic_ns())
            self.stats.dequeued += 1
            self.stats.total_wait_ms += (now - item.enqueue_ns) / 1e6
            self.stats.depth = len(self._items)
            self._cond.notify_all()
            return item.value

    def oldest_age_ms(self) -> float:
        with self._cond:
            if not self._items:
                return 0.0
            return (time.monotonic_ns() - self._items[0].enqueue_ns) / 1e6

    def depth(self) -> int:
        with self._cond:
            return len(self._items)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def drain(self) -> int:
        with self._cond:
            n = len(self._items)
            self._items.clear()
            self.stats.depth = 0
            self._cond.notify_all()
            return n

    def snapshot(self) -> dict:
        with self._cond:
            self.stats.oldest_frame_age_ms = round(self.oldest_age_ms(), 4)
            return self.stats.as_dict() | {"name": self.name, "policy": self._policy}
