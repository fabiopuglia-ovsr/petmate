"""Tiny in-process sliding-window rate limiter (per client IP + bucket).
Behind Caddy the client IP is taken from X-Forwarded-For (trusted proxy)."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self.enabled = True

    def check(self, key: str, calls: int, period_s: float) -> float | None:
        """Return None if allowed, else seconds to wait."""
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > period_s:
                q.popleft()
            if len(q) >= calls:
                return period_s - (now - q[0])
            q.append(now)
            if len(self._hits) > 50_000:  # crude memory guard
                self._hits.clear()
        return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def limit(bucket: str, setting_attr: str):
    """FastAPI dependency factory: `Depends(limit("auth", "rate_auth"))`."""

    def dep(request: Request) -> None:
        settings = request.app.state.settings
        limiter: RateLimiter = request.app.state.limiter
        calls, period = getattr(settings, setting_attr)
        wait = limiter.check(f"{bucket}:{client_ip(request)}", calls, period)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="too many requests",
                headers={"Retry-After": str(max(1, int(wait + 0.999)))},
            )

    return dep
