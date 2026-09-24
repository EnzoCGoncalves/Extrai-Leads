import asyncio
import pickle
from collections import OrderedDict
from dataclasses import dataclass
from time import monotonic
from typing import Any

from extrais_leads.cache.base import CacheBackend


@dataclass(slots=True)
class _CacheEntry:
    payload: bytes
    size: int
    expires_at: float


class MemoryCache(CacheBackend):
    """Process-local, concurrency-safe TTL cache for the first version."""

    def __init__(
        self,
        default_ttl_seconds: int = 86_400,
        max_entries: int = 500,
        max_bytes: int = 33_554_432,
    ) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("default_ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._default_ttl = default_ttl_seconds
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._current_bytes = 0
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    def _remove_expired(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._pop(key)

    def _pop(self, key: str) -> _CacheEntry | None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._current_bytes -= entry.size
        return entry

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= monotonic():
                self._pop(key)
                return None
            self._entries.move_to_end(key)
            try:
                return pickle.loads(entry.payload)
            except (pickle.PickleError, EOFError, AttributeError, ValueError):
                self._pop(key)
                return None

    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self._default_ttl if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        size = len(payload)
        async with self._lock:
            now = monotonic()
            self._remove_expired(now)
            self._pop(key)
            if size > self._max_bytes:
                return
            while self._entries and (
                len(self._entries) >= self._max_entries
                or self._current_bytes + size > self._max_bytes
            ):
                oldest_key = next(iter(self._entries))
                self._pop(oldest_key)
            self._entries[key] = _CacheEntry(
                payload=payload,
                size=size,
                expires_at=now + ttl,
            )
            self._current_bytes += size

    async def delete(self, key: str) -> bool:
        async with self._lock:
            return self._pop(key) is not None

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
            self._current_bytes = 0

    @property
    def current_bytes(self) -> int:
        return self._current_bytes
