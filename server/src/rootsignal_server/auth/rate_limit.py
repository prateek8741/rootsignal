"""In-process token-bucket rate limiting per API key.

Limits apply per server process; with several processes the effective limit
multiplies. Acceptable for a single-node beta; to be recorded in THREAT_MODEL.md
(planned before increment 6) along with the other single-node assumptions.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, rate_per_second: float, burst: int, *,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 100_000) -> None:
        self._rate = rate_per_second
        self._burst = float(burst)
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: dict[Hashable, _Bucket] = {}
        self._lock = threading.Lock()

    def try_acquire(self, key_id: Hashable) -> float:
        """Take one token. Returns 0 on success, else seconds until one is available."""
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key_id)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    self._buckets.clear()  # crude bound on memory; resets all buckets
                bucket = self._buckets[key_id] = _Bucket(self._burst, now)
            bucket.tokens = min(self._burst, bucket.tokens + (now - bucket.updated) * self._rate)
            bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return 0.0
            return (1.0 - bucket.tokens) / self._rate
