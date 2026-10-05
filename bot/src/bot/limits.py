"""Small abuse guard for non-trusted users."""

from __future__ import annotations

import time
from collections.abc import Callable


class Cooldown:
    """Allow one message per `seconds` per user. check() returns seconds to wait (0 = go)."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._last: dict[int, float] = {}

    def check(self, user_id: int) -> float:
        now = self._clock()
        last = self._last.get(user_id)
        if last is not None and now - last < self.seconds:
            return self.seconds - (now - last)
        self._last[user_id] = now
        return 0.0
