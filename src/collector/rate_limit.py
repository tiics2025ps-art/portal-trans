from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class DelayEvent:
    seconds: float
    kind: str


class SerialRateLimiter:
    """Garante uma requisição por vez e espaçamento mínimo entre requisições."""

    def __init__(
        self,
        min_delay: float,
        max_delay: float,
        pause_every_downloads: int = 10,
        min_pause: float = 300,
        max_pause: float = 600,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[float, float], float] = random.uniform,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.pause_every_downloads = pause_every_downloads
        self.min_pause = min_pause
        self.max_pause = max_pause
        self.sleep_fn = sleep_fn
        self.random_fn = random_fn
        self.monotonic_fn = monotonic_fn
        self._mutex = threading.Lock()
        self._last_request_finished: float | None = None
        self._download_count = 0

    def before_request(self) -> DelayEvent | None:
        self._mutex.acquire()
        if self._last_request_finished is None:
            return None
        target = self.random_fn(self.min_delay, self.max_delay)
        elapsed = self.monotonic_fn() - self._last_request_finished
        remaining = max(0.0, target - elapsed)
        if remaining:
            self.sleep_fn(remaining)
        return DelayEvent(remaining, "request_delay")

    def after_request(self) -> None:
        self._last_request_finished = self.monotonic_fn()
        self._mutex.release()

    def after_download(self) -> DelayEvent | None:
        self._download_count += 1
        if self.pause_every_downloads <= 0:
            return None
        if self._download_count % self.pause_every_downloads != 0:
            return None
        seconds = self.random_fn(self.min_pause, self.max_pause)
        self.sleep_fn(seconds)
        return DelayEvent(seconds, "periodic_pause")

    def request_slot(self):
        limiter = self

        class _Slot:
            def __enter__(self):
                return limiter.before_request()

            def __exit__(self, exc_type, exc, tb):
                limiter.after_request()
                return False

        return _Slot()
