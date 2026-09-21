import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any

from extrais_leads.cache.base import CacheBackend


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    expires_at: float


class MemoryCache(CacheBackend):
    """Process-local, concurrency-safe TTL cache for the first version."""

    def __init__(self, default_ttl_seconds: int = 86_400, max_entries: int = 10_000) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._default_ttl = default_ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    def _remove_expired(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= monotonic():
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry.value

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self._default_ttl if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        async with self._lock:
            now = monotonic()
            self._remove_expired(now)
            self._entries.pop(key, None)
            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
            self._entries[key] = _CacheEntry(value=value, expires_at=now + ttl)

    async def delete(self, key: str) -> bool:
        async with self._lock:
            return self._entries.pop(key, None) is not None

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
