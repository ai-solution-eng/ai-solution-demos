"""Stat-keyed file hashing for media caches.

Audit 2026-10-16, P2 (cache keying).  ``mcp_server`` kept a path-keyed
``_file_hash_cache``: the key was the PATH, so a file REPLACED at the same
path (a re-upload, a regenerated ``*_preprocessed`` sibling, a repair pass)
kept serving the old digest forever — stale cache hits on the query-vector
and media paths, with no invalidation hook anywhere.

The key here is ``(path, st_mtime_ns, st_size)``, so a rewrite is a MISS by
construction and needs no invalidation call at all.  ``st_mtime_ns`` (not
``st_mtime``) is deliberate: two writes inside the same float second are
indistinguishable at float resolution, and a same-size rewrite is exactly
the case a size-only key would miss.

Design notes:

* Bounded, insertion-ordered dict with move-to-back on hit (LRU), so a
  long-running server cannot accumulate one entry per file ever seen.
* The digest is computed OUTSIDE the lock: hashing a multi-MB file while
  holding a process-wide lock would serialise every concurrent reader.
  Two threads may therefore hash the same file simultaneously; the loser's
  identical result is simply overwritten.
* A stat failure (missing file, permissions) propagates to the caller as
  ``OSError`` — callers decide whether that is fatal.  Nothing is cached
  for a path that could not be stat-ed.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any

#: Default LRU bound, matching the mcp_server cache caps it replaces.
DEFAULT_MAX_ENTRIES = 4096

#: Read size for the streaming digest.
_CHUNK = 65536


class StatKeyedFileHasher:
    """SHA-256 of a file, cached under ``(path, st_mtime_ns, st_size)``.

    Parameters
    ----------
    max_entries:
        LRU bound.  Entries beyond it are evicted oldest-first.
    digest_fn:
        Injectable digest factory (tests pass a counting/spy hashlib
        constructor).  Defaults to :func:`hashlib.sha256`.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES, digest_fn: Any = None) -> None:
        self._max_entries = max(1, int(max_entries))
        self._digest_fn = digest_fn or hashlib.sha256
        self._cache: dict[tuple[str, int, int], str] = {}
        self._lock = threading.Lock()
        self._hashes = 0  # files actually read (test/metrics visibility)

    @staticmethod
    def stat_key(path: str | Path) -> tuple[str, int, int]:
        """The cache key for *path*: ``(path, st_mtime_ns, st_size)``.

        Raises ``OSError`` when the file cannot be stat-ed — callers must not
        treat a missing file as a cache hit.
        """
        p = str(path)
        st = os.stat(p)
        return (p, int(st.st_mtime_ns), int(st.st_size))

    def digest(self, path: str | Path) -> str:
        """Return the cached-or-computed SHA-256 hex digest of *path*.

        A file whose ``(mtime_ns, size)`` pair changed since it was last
        hashed is re-read; an unchanged file is served from the cache.
        """
        key = self.stat_key(path)
        with self._lock:
            hit = self._cache.pop(key, None)
            if hit is not None:
                self._cache[key] = hit  # move-to-back: LRU on eviction
                return hit
        digest = self._compute(str(path))
        with self._lock:
            self._cache.pop(key, None)
            self._cache[key] = digest
            while len(self._cache) > self._max_entries:
                self._cache.pop(next(iter(self._cache)))
        return digest

    def _compute(self, path: str) -> str:
        """Stream *path* through the digest (never buffered whole)."""
        h = self._digest_fn()
        with open(path, "rb") as f:
            while chunk := f.read(_CHUNK):
                h.update(chunk)
        self._hashes += 1
        return h.hexdigest()

    # -- introspection (tests / metrics) --------------------------------

    def clear(self) -> None:
        """Drop every cached entry (the cache never needs this to be correct)."""
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def files_hashed(self) -> int:
        """How many times a file was actually read (cache misses)."""
        return self._hashes


#: Process-wide default hasher for callers that want the shared cache.
_DEFAULT = StatKeyedFileHasher()


def file_digest(path: str | Path) -> str:
    """SHA-256 of *path* via the process-wide stat-keyed cache.

    Convenience wrapper; callers that need their own bound or a spy digest
    should build a :class:`StatKeyedFileHasher` instead.
    """
    return _DEFAULT.digest(path)


def digest_cache_stats() -> dict[str, int]:
    """Size + miss count of the process-wide hasher (for /metrics-style reads)."""
    return {"entries": len(_DEFAULT), "files_hashed": _DEFAULT.files_hashed}
