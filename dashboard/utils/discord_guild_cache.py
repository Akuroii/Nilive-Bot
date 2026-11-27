"""Small process-local TTL cache for stable Discord guild metadata.

Keys contain only a one-way token fingerprint, never the bot token. A per-key
lock coalesces concurrent cold-cache requests. Failed/malformed fetches are
never cached; callers can force a refresh with ``?refresh=1``.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Callable, TypeVar

T = TypeVar("T")
_CACHE_TTL_SECONDS = 20.0
_CACHE: dict[tuple[str, str, str], tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()
_KEY_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}


def _identity(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_or_load(kind: str, guild_id: int | str, token: str,
                loader: Callable[[], tuple[T, bool]], *, refresh: bool = False) -> T:
    """Return cached data or load it once; ``bool`` indicates cacheability."""
    key = (kind, str(guild_id), _identity(token))
    with _CACHE_LOCK:
        lock = _KEY_LOCKS.setdefault(key, threading.Lock())
    with lock:
        now = time.monotonic()
        if refresh:
            with _CACHE_LOCK:
                _CACHE.pop(key, None)
        if not refresh:
            with _CACHE_LOCK:
                cached = _CACHE.get(key)
                if cached and cached[0] > now:
                    return cached[1]  # type: ignore[return-value]
                if cached:
                    _CACHE.pop(key, None)
        value, cacheable = loader()
        if cacheable:
            with _CACHE_LOCK:
                _CACHE[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)
        return value


def clear_cache() -> None:
    """Test helper; does not affect Discord or persisted application state."""
    with _CACHE_LOCK:
        _CACHE.clear()
