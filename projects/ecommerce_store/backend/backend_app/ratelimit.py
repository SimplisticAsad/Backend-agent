"""Tiny fixed-window in-process rate limiter for credential endpoints (brute-force protection)."""
from __future__ import annotations

import threading
import time

from .errors import RateLimited


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.limit = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < 60]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                raise RateLimited(headers={"Retry-After": str(int(60 - (now - hits[0])) + 1)})
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 10_000:  # bound memory
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}
