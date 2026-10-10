"""Thread-safe in-memory caching with Time-To-Live (TTL) and LRU capacity limits."""

import threading
import time
from typing import Any, Dict, Optional, Tuple

from app.config import (
    BOUNDARY_CACHE_TTL_SECONDS,
    CACHE_TTL_SECONDS,
)


class TTLCache:
    """Thread-safe in-memory cache supporting TTL expiration and maximum capacity."""

    def __init__(self, default_ttl: float = 600.0, max_size: int = 256):
        self.default_ttl = default_ttl
        self.max_size = max_size
        self._store: Dict[Any, Tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def get(self, key: Any, default: Any = None) -> Any:
        now = time.monotonic()
        with self._lock:
            if key not in self._store:
                return default
            expires_at, value = self._store[key]
            if now >= expires_at:
                del self._store[key]
                return default
            return value

    def set(self, key: Any, value: Any, ttl: Optional[float] = None) -> None:
        effective_ttl = self.default_ttl if ttl is None else ttl
        expires_at = time.monotonic() + effective_ttl
        with self._lock:
            if len(self._store) >= self.max_size and key not in self._store:
                self._evict_expired_or_oldest()
            self._store[key] = (expires_at, value)

    def delete(self, key: Any) -> bool:
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)

    def _evict_expired_or_oldest(self) -> None:
        now = time.monotonic()
        # First remove any already expired items
        expired = [k for k, (exp, _) in self._store.items() if exp <= now]
        for k in expired:
            del self._store[k]
        # If still at or over capacity, remove the earliest expiring item
        if len(self._store) >= self.max_size:
            oldest_key = min(self._store.keys(), key=lambda k: self._store[k][0])
            del self._store[oldest_key]


# Shared cache instances
boundary_cache = TTLCache(default_ttl=BOUNDARY_CACHE_TTL_SECONDS, max_size=200)
query_cache = TTLCache(default_ttl=CACHE_TTL_SECONDS, max_size=100)
metadata_cache = TTLCache(default_ttl=3600.0, max_size=50)
