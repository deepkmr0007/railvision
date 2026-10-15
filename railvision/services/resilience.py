"""Caching, rate limiting, circuit breaking and backoff — in-process implementations.

Interfaces are deliberately tiny so a Redis-backed implementation can replace them in
multi-worker deployments (see docs/ARCHITECTURE.md).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable


class TTLCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._d: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if not v:
                return None
            if v[0] < self._clock():
                self._d.pop(key, None)
                return None
            return v[1]

    def set(self, key, value, ttl: float):
        with self._lock:
            self._d[key] = (self._clock() + ttl, value)


class TokenBucket:
    """`rate` tokens per `per` seconds, burst up to `capacity`."""

    def __init__(self, rate: float, per: float = 60.0, capacity: float | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.rate, self.per = rate, per
        self.capacity = capacity if capacity is not None else rate
        self._tokens = self.capacity
        self._t = clock()
        self._clock = clock
        self._lock = threading.Lock()

    def try_take(self, n: float = 1.0) -> bool:
        with self._lock:
            now = self._clock()
            self._tokens = min(self.capacity, self._tokens + (now - self._t) * self.rate / self.per)
            self._t = now
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False


class KeyedRateLimiter:
    """Per-key token buckets (per IP, per user, per phone number)."""

    def __init__(self, rate: float, per: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.rate, self.per, self._clock = rate, per, clock
        self._b: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            b = self._b.get(key)
            if b is None:
                b = self._b[key] = TokenBucket(self.rate, self.per, clock=self._clock)
        return b.try_take()


class CircuitBreaker:
    """CLOSED -> OPEN after `threshold` consecutive failures; HALF_OPEN after `cooldown` s."""

    def __init__(self, threshold: int = 4, cooldown: float = 120.0, clock: Callable[[], float] = time.monotonic):
        self.threshold, self.cooldown, self._clock = threshold, cooldown, clock
        self.failures = 0
        self.opened_at: float | None = None
        self.last_error: str | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "CLOSED"
        return "HALF_OPEN" if self._clock() - self.opened_at >= self.cooldown else "OPEN"

    def allow(self) -> bool:
        return self.state != "OPEN"

    def success(self):
        self.failures, self.opened_at, self.last_error = 0, None, None

    def failure(self, err: str = ""):
        self.failures += 1
        self.last_error = err[:300]
        if self.failures >= self.threshold:
            self.opened_at = self._clock()


def backoff_seconds(attempt: int, base: float = 60.0, cap: float = 1800.0) -> float:
    """Exponential backoff (no jitter, deterministic for tests): 60, 120, 240, ... capped."""
    return min(cap, base * (2 ** max(0, attempt - 1)))
