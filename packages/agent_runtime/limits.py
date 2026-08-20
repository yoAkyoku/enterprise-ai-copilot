"""Rate-limit adapters for local and multi-replica deployments."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict, deque
from typing import Protocol


class RateLimiter(Protocol):
    def check(self, key: str) -> tuple[bool, int]:
        """Return whether an event is allowed and a retry-after value."""


class InMemoryRateLimiter:
    """Fixed-window event limiter with bounded per-key history."""

    def __init__(self, max_events: int, window_seconds: int = 60) -> None:
        if max_events <= 0 or window_seconds <= 0:
            raise ValueError("rate limit values must be positive")
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.max_events:
                retry_after = max(1, int(events[0] + self.window_seconds - now + 0.999))
                return False, retry_after
            events.append(now)
            return True, 0


class RedisRateLimiter:
    """Atomic fixed-window limiter backed by a Redis-compatible client.

    The client is injected so the domain package does not own credentials or
    connection construction. Redis failures fail closed for the protected
    endpoint instead of silently bypassing abuse controls.
    """

    _SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
if count > tonumber(ARGV[2]) then return {0, redis.call('TTL', KEYS[1])} end
return {1, 0}
"""

    def __init__(
        self, client: object, max_events: int, *, window_seconds: int = 60, prefix: str = "agent"
    ) -> None:
        if max_events <= 0 or window_seconds <= 0 or not prefix.strip():
            raise ValueError("rate limit values must be positive")
        self.client = client
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.prefix = prefix.strip()

    def check(self, key: str) -> tuple[bool, int]:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        redis_key = f"{self.prefix}:rate:{digest}"
        try:
            result = self.client.eval(  # type: ignore[attr-defined]
                self._SCRIPT,
                1,
                redis_key,
                self.window_seconds,
                self.max_events,
            )
            allowed = bool(result[0])
            retry_after = max(1, int(result[1])) if not allowed else 0
            return allowed, retry_after
        except Exception:  # noqa: BLE001 - any backend outage must fail closed
            return False, self.window_seconds
