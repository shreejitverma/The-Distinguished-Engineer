"""A common half-fix: latency is measured from the intended send time, but the
schedule is rebuilt from "now" whenever the generator falls behind.

This is what a rate limiter with no burst allowance does. It still skips every
send that the stall swallowed.
"""

from __future__ import annotations


def run(clock, service, histogram, rate_per_s: int, duration_us: int) -> int:
    interval = 1_000_000 // rate_per_s
    end = clock.now() + duration_us
    next_send = clock.now()
    sent = 0
    while next_send < end:
        clock.sleep_until(next_send)
        service.call(clock)
        histogram.record_value(clock.now() - next_send)
        sent += 1
        next_send = max(next_send + interval, clock.now())   # the backlog is dropped here
    return sent
