from __future__ import annotations

import threading
import time

from collector.rate_limit import SerialRateLimiter


def test_interval_is_between_25_and_45_seconds_without_real_sleep() -> None:
    sleeps: list[float] = []
    clock = iter([0.0, 0.0, 0.0])
    limiter = SerialRateLimiter(
        25,
        45,
        sleep_fn=sleeps.append,
        random_fn=lambda a, b: 31.5,
        monotonic_fn=lambda: next(clock),
    )
    with limiter.request_slot():
        pass
    with limiter.request_slot() as event:
        assert event is not None
        assert 25 <= event.seconds <= 45
    assert sleeps == [31.5]


def test_periodic_pause_after_ten_downloads() -> None:
    sleeps: list[float] = []
    limiter = SerialRateLimiter(
        0,
        0,
        pause_every_downloads=10,
        min_pause=300,
        max_pause=600,
        sleep_fn=sleeps.append,
        random_fn=lambda a, b: 420,
    )
    for _ in range(9):
        assert limiter.after_download() is None
    event = limiter.after_download()
    assert event is not None
    assert event.seconds == 420
    assert sleeps == [420]


def test_only_one_request_slot_at_a_time() -> None:
    limiter = SerialRateLimiter(0, 0)
    active = 0
    peak = 0
    mutex = threading.Lock()

    def worker() -> None:
        nonlocal active, peak
        with limiter.request_slot():
            with mutex:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with mutex:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1
