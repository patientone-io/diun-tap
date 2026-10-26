"""Deduplication cache with bounded memory (TTLCache + lock)."""

from __future__ import annotations

import asyncio

from cachetools import TTLCache


class DedupCache:
    def __init__(self, maxsize: int = 512, ttl: int = 30) -> None:
        self._cache: TTLCache[str, float] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = asyncio.Lock()
        self._pending: set[str] = set()

    async def reserve(self, key: str) -> bool:
        """Reserve one delivery; active reservations do not expire mid-send."""
        async with self._lock:
            if key in self._pending or key in self._cache:
                return False
            self._pending.add(key)
            return True

    async def finish(self, key: str, delivered: bool) -> None:
        """Release a reservation, caching only confirmed successful deliveries."""
        async with self._lock:
            self._pending.discard(key)
            if delivered:
                self._cache[key] = 1.0

    async def check_and_mark(self, key: str) -> bool:
        """Return True when the key is a duplicate within the window."""
        async with self._lock:
            if key in self._cache:
                return True
            self._cache[key] = 1.0
            return False

    async def __len__(self) -> int:
        async with self._lock:
            return len(self._cache)
