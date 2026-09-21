from abc import ABC, abstractmethod
from typing import Any


class CacheBackend(ABC):
    """Storage-independent asynchronous cache contract."""

    @abstractmethod
    async def get(self, key: str) -> Any | None:
        raise NotImplementedError

    @abstractmethod
    async def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete(self, key: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def clear(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        """Release resources for backends that own external connections."""
        return None
