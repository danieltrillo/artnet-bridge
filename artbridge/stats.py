"""Tiny counters with a packets-per-second estimate."""

from __future__ import annotations

import time


class RateCounter:
    __slots__ = ("total", "rate", "last", "_window", "_window_start")

    def __init__(self) -> None:
        self.total = 0
        self.rate = 0.0
        self.last = 0.0
        self._window = 0
        self._window_start = time.monotonic()

    def hit(self, now: float | None = None) -> None:
        self.total += 1
        self._window += 1
        self.last = now if now is not None else time.monotonic()

    def tick(self, now: float) -> None:
        elapsed = now - self._window_start
        if elapsed <= 0:
            return
        self.rate = self._window / elapsed
        self._window = 0
        self._window_start = now

    def age(self, now: float) -> float | None:
        return None if not self.last else now - self.last

    def to_dict(self, now: float) -> dict:
        return {
            "total": self.total,
            "pps": round(self.rate, 1),
            "age": None if not self.last else round(now - self.last, 1),
        }
