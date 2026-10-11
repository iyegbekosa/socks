import math
import time

from django.core.cache import cache


class RateLimit:
    """Fixed-window failure counter backed by the (schema-prefixed) cache."""

    def __init__(self, scope: str, limit: int = 5, window: int = 900) -> None:
        self.scope = scope
        self.limit = limit
        self.window = window

    def _count_key(self, key: str) -> str:
        return f"rl:{self.scope}:{key}"

    def _expiry_key(self, key: str) -> str:
        return f"rl:{self.scope}:{key}:exp"

    def hit(self, key: str) -> int:
        count_key = self._count_key(key)
        if cache.add(count_key, 1, timeout=self.window):
            cache.set(self._expiry_key(key), time.time() + self.window, timeout=self.window)
            return 1
        try:
            return int(cache.incr(count_key))
        except ValueError:  # expired between add and incr
            cache.set(count_key, 1, timeout=self.window)
            cache.set(self._expiry_key(key), time.time() + self.window, timeout=self.window)
            return 1

    def count(self, key: str) -> int:
        return int(cache.get(self._count_key(key), 0))

    def is_blocked(self, key: str) -> bool:
        return self.count(key) >= self.limit

    def retry_after(self, key: str) -> int:
        expires = cache.get(self._expiry_key(key))
        if expires is None:
            return 0 if not self.is_blocked(key) else self.window
        return max(1, min(self.window, math.ceil(expires - time.time())))

    def release(self, key: str) -> None:
        """Give back one hit (used when an attempt turns out to be legitimate)."""
        try:
            if int(cache.decr(self._count_key(key))) <= 0:
                self.reset(key)
        except ValueError:  # already expired
            pass

    def reset(self, key: str) -> None:
        cache.delete_many([self._count_key(key), self._expiry_key(key)])
