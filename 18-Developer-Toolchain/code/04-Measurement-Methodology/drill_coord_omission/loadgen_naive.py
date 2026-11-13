"""The load generator under investigation. Read this file first.

It paces itself to the target rate, sends one request at a time, and times each
request from the moment it is sent. Every line looks reasonable.
"""

from __future__ import annotations


def run(clock, service, histogram, rate_per_s: int, duration_us: int) -> int:
    """Drive `service` at `rate_per_s` for `duration_us`; return requests sent."""
    interval = 1_000_000 // rate_per_s
    end = clock.now() + duration_us
    sent = 0
    while clock.now() < end:
        start = clock.now()
        service.call(clock)                      # blocks until the response arrives
        histogram.record_value(clock.now() - start)
        sent += 1
        clock.sleep_until(start + interval)      # pace to one request per interval
    return sent
