from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass
class _CacheItem:
    value: Any
    expires_at: float


class PerformanceRuntime:
    def __init__(self) -> None:
        self._cache: dict[str, _CacheItem] = {}
        self._lock = asyncio.Lock()
        self.cache_hits = 0
        self.cache_misses = 0
        self.started_at = time.monotonic()

    async def cached(self, key: str, ttl: float, factory: Callable[[], Awaitable[Any]]) -> Any:
        now = time.monotonic()
        item = self._cache.get(key)
        if item and item.expires_at > now:
            self.cache_hits += 1
            return item.value
        self.cache_misses += 1
        async with self._lock:
            now = time.monotonic()
            item = self._cache.get(key)
            if item and item.expires_at > now:
                self.cache_hits += 1
                return item.value
            value = await factory()
            self._cache[key] = _CacheItem(value=value, expires_at=now + max(1.0, ttl))
            return value

    def clear(self) -> int:
        count = len(self._cache)
        self._cache.clear()
        return count

    def stats(self) -> dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "entries": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": (self.cache_hits / total * 100.0) if total else 0.0,
            "uptime_seconds": int(time.monotonic() - self.started_at),
        }


performance_runtime = PerformanceRuntime()
