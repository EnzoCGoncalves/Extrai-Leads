"""Short-lived Overture scan worker used to release native Arrow memory."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from typing import NoReturn

from extrais_leads.core.memory import current_rss_mb, peak_rss_mb
from extrais_leads.providers.base import ProviderSearchRequest
from extrais_leads.providers.overture import _WORKER_RESULT_PREFIX, OvertureMapsProvider


async def _unused_location_resolver(_location: str) -> NoReturn:
    raise RuntimeError("worker location resolver must not be called")


def main() -> None:
    try:
        # The worker is intentionally single-purpose; the system allocator and
        # one Arrow thread avoid allocator arenas/thread stacks that outlive a scan.
        os.environ.setdefault("ARROW_DEFAULT_MEMORY_POOL", "system")
        payload = json.loads(sys.stdin.buffer.read())
        request = ProviderSearchRequest.model_validate(payload["request"])
        area = SimpleNamespace(**payload["area"])
        settings = payload["settings"]
        provider = OvertureMapsProvider(
            _unused_location_resolver,
            min_confidence=settings["min_confidence"],
            connect_timeout_seconds=settings["connect_timeout_seconds"],
            request_timeout_seconds=settings["request_timeout_seconds"],
            use_stac=settings["use_stac"],
            isolate_process=False,
        )
        category = provider._match_category(request.category or request.query)
        if category is None:
            raise ValueError("unsupported category")
        page = provider._search_region(request, category, area)
        envelope = {
            "page": page.model_dump(mode="json"),
            "end_rss_mb": _rounded(current_rss_mb()),
            "peak_rss_mb": _rounded(peak_rss_mb()),
        }
        print(
            _WORKER_RESULT_PREFIX + json.dumps(envelope, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
    except Exception as exc:
        print(f"Overture worker failed: {type(exc).__name__}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


if __name__ == "__main__":
    main()
