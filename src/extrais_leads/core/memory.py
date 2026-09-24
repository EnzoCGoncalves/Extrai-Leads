"""Lightweight process-memory telemetry without optional runtime dependencies."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from extrais_leads.core.logging import log_event


def current_rss_mb() -> float | None:
    """Return current resident memory when the operating system exposes it."""

    try:
        if sys.platform.startswith("linux"):
            resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576
        if sys.platform == "win32":
            return _windows_memory_mb()[0]
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        divisor = 1_048_576 if sys.platform == "darwin" else 1_024
        return usage.ru_maxrss / divisor
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def peak_rss_mb() -> float | None:
    """Return process peak RSS, primarily for bounded worker diagnostics."""

    try:
        if sys.platform == "win32":
            return _windows_memory_mb()[1]
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        divisor = 1_048_576 if sys.platform == "darwin" else 1_024
        return usage.ru_maxrss / divisor
    except (OSError, ValueError, AttributeError):
        return None


def log_memory(logger: logging.Logger, stage: str, **context: Any) -> None:
    rss = current_rss_mb()
    log_event(
        logger,
        "MEMORY",
        "Uso de memória do processo",
        stage=stage,
        rss_mb=round(rss, 1) if rss is not None else None,
        **context,
    )


def _windows_memory_mb() -> tuple[float, float]:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCounters),
        wintypes.DWORD,
    )
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), ctypes.sizeof(counters)):
        raise OSError("GetProcessMemoryInfo failed")
    return (
        counters.WorkingSetSize / 1_048_576,
        counters.PeakWorkingSetSize / 1_048_576,
    )
