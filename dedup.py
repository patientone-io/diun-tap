"""Deduplication cache with bounded memory (TTLCache + lock)."""

from __future__ import annotations

import asyncio

from cachetools import TTLCache


class DedupCache:
    def __init__(self, maxsize: int = 512, ttl: int = 30) -> None:
        self._cache: TTLCache[str, float] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = asyncio.Lock()

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
