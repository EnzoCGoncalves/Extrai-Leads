import pytest

from extrais_leads.cache.memory import MemoryCache


@pytest.mark.asyncio
async def test_memory_cache_hit_delete_and_expiration(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr("extrais_leads.cache.memory.monotonic", lambda: clock[0])
    cache = MemoryCache(default_ttl_seconds=60)

    assert await cache.get("query") is None
    await cache.set("query", {"results": 3}, ttl_seconds=10)
    assert await cache.get("query") == {"results": 3}

    clock[0] = 111.0
    assert await cache.get("query") is None

    await cache.set("other", "value")
    assert await cache.delete("other") is True
    assert await cache.delete("other") is False


@pytest.mark.asyncio
async def test_memory_cache_is_bounded_and_can_be_cleared() -> None:
    cache = MemoryCache(default_ttl_seconds=60, max_entries=2)
    with pytest.raises(ValueError):
        await cache.set("invalid", "value", ttl_seconds=0)
    await cache.set("first", 1)
    await cache.set("second", 2)
    assert await cache.get("first") == 1

    await cache.set("third", 3)
    assert await cache.get("second") is None
    assert await cache.get("first") == 1
    assert await cache.get("third") == 3

    await cache.clear()
    assert await cache.get("first") is None
    assert await cache.get("third") is None


@pytest.mark.asyncio
async def test_memory_cache_enforces_serialized_byte_budget() -> None:
    cache = MemoryCache(default_ttl_seconds=60, max_entries=100, max_bytes=220)

    await cache.set("first", "a" * 120)
    await cache.set("second", "b" * 120)

    assert cache.current_bytes <= 220
    assert await cache.get("first") is None
    assert await cache.get("second") == "b" * 120

    await cache.set("oversized", "c" * 500)
    assert await cache.get("oversized") is None
    assert cache.current_bytes <= 220


def test_memory_cache_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        MemoryCache(default_ttl_seconds=0)
    with pytest.raises(ValueError):
        MemoryCache(max_entries=0)
    with pytest.raises(ValueError):
        MemoryCache(max_bytes=0)
