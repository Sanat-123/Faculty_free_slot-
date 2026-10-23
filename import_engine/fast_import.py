"""
fast_import.py - speed helpers for timetable imports
====================================================

Two independent, fully generic speed-ups (nothing here knows any
file name, teacher, subject, room or class):

1. RESULT CACHE
   The finished result of importing a file is stored on disk under a
   key made from the file's CONTENT (sha256), its base name and a
   fingerprint of the importer's own source code. Uploading the same
   file again - even after restarting the app - returns instantly.
   Because the importer's source code is part of the key, editing the
   parser automatically invalidates every old entry, so a cached
   result can never be stale.

2. SHARED PROCESS POOL
   Reading PDF pages with pdfminer/pdfplumber is pure-Python CPU work
   and the pages of a timetable PDF are independent of each other.
   A lazily created, reusable pool of worker processes lets the
   importer process many pages at the same time (one worker per CPU
   core). Any problem with the pool falls back to the original
   sequential code, so behaviour never changes - only speed.

Environment switches (all optional):

    FACULTY_IMPORT_CACHE=0       disable the result cache
    FACULTY_IMPORT_WORKERS=N     number of worker processes
                                 (1 = no parallelism)
    FACULTY_IMPORT_CACHE_DIR=... where cache files are stored
"""

from __future__ import annotations

import atexit
import hashlib
import os
import pickle
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CACHE_ENABLED = os.environ.get("FACULTY_IMPORT_CACHE", "1") != "0"

CACHE_DIR = Path(
    os.environ.get(
        "FACULTY_IMPORT_CACHE_DIR",
        str(PROJECT_ROOT / ".import_cache"),
    )
)

# Only parallelise PDFs that have at least this many pages -
# for tiny files the process hand-off costs more than it saves.
MIN_PAGES_FOR_PARALLEL = 8


# ==========================================================
# CODE FINGERPRINT (cache invalidation)
# ==========================================================

_code_version: Optional[str] = None


def code_version() -> str:
    """Hash of the importer's source files (computed once)."""

    global _code_version

    if _code_version is not None:
        return _code_version

    digest = hashlib.sha256()

    for folder in ("import_engine", "parser", "utils"):

        base = PROJECT_ROOT / folder

        if not base.is_dir():
            continue

        for source in sorted(base.glob("*.py")):

            if "backup" in source.name.lower():
                continue

            try:
                digest.update(source.name.encode("utf-8"))
                digest.update(source.read_bytes())
            except OSError:
                continue

    _code_version = digest.hexdigest()[:16]

    return _code_version


# ==========================================================
# RESULT CACHE
# ==========================================================

def _cache_path(file_path: str | Path) -> Optional[Path]:

    path = Path(file_path)

    try:
        digest = hashlib.sha256()

        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)

        # the base name is part of the key because it is stored in
        # every record (source_file) and is used as a fallback
        # identity hint - a renamed copy is simply re-imported.
        digest.update(path.name.encode("utf-8"))
        digest.update(code_version().encode("ascii"))

        return CACHE_DIR / f"{digest.hexdigest()}.pkl"

    except OSError:
        return None


def load_cached(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """Return a previously stored import result, or None."""

    if not CACHE_ENABLED:
        return None

    cache_file = _cache_path(file_path)

    if cache_file is None or not cache_file.is_file():
        return None

    try:
        with open(cache_file, "rb") as handle:
            result = pickle.load(handle)

        if isinstance(result, dict) and result.get("success"):
            return result

    except Exception:
        # corrupted / unreadable cache entry -> ignore it
        try:
            cache_file.unlink()
        except OSError:
            pass

    return None


def store_cached(file_path: str | Path, result: Dict[str, Any]) -> None:
    """Persist a SUCCESSFUL import result (never fails the import)."""

    if not CACHE_ENABLED:
        return

    if not (isinstance(result, dict) and result.get("success")):
        return

    cache_file = _cache_path(file_path)

    if cache_file is None:
        return

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # write atomically so a crash can't leave a half-written file
        fd, tmp_name = tempfile.mkstemp(
            dir=str(CACHE_DIR), suffix=".tmp"
        )

        with os.fdopen(fd, "wb") as handle:
            pickle.dump(result, handle, protocol=pickle.HIGHEST_PROTOCOL)

        os.replace(tmp_name, cache_file)

    except Exception:
        pass


def clear_cache() -> int:
    """Delete every cached result. Returns how many were removed."""

    removed = 0

    if CACHE_DIR.is_dir():
        for entry in CACHE_DIR.glob("*.pkl"):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass

    return removed


# ==========================================================
# SHARED PROCESS POOL
# ==========================================================

def worker_count() -> int:

    try:
        configured = int(os.environ.get("FACULTY_IMPORT_WORKERS", "0"))
    except ValueError:
        configured = 0

    if configured > 0:
        return configured

    return max(1, os.cpu_count() or 1)


_pool: Optional[ProcessPoolExecutor] = None


def get_pool() -> Optional[ProcessPoolExecutor]:
    """Lazily create ONE pool and reuse it for every import."""

    global _pool

    if worker_count() <= 1:
        return None

    if _pool is None:

        try:
            _pool = ProcessPoolExecutor(max_workers=worker_count())
            atexit.register(shutdown_pool)
        except Exception:
            _pool = None

    return _pool


def shutdown_pool() -> None:

    global _pool

    if _pool is not None:
        try:
            _pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        _pool = None


def _noop() -> int:
    return os.getpid()


def warm_up() -> None:
    """
    Start the worker processes in the background (optional).
    Call once at application start so the first upload doesn't pay
    the process start-up cost (noticeable on Windows).
    """

    pool = get_pool()

    if pool is None:
        return

    try:
        for _ in range(worker_count()):
            pool.submit(_noop)
    except Exception:
        shutdown_pool()


def split_pages(page_count: int, workers: int) -> list[list[int]]:
    """
    Split pages 1..page_count into contiguous chunks - about four
    chunks per worker so a slow chunk doesn't leave cores idle.
    """

    size = max(1, -(-page_count // (workers * 4)))

    return [
        list(range(start, min(start + size, page_count + 1)))
        for start in range(1, page_count + 1, size)
    ]