"""The model solution: a static schedule and latency measured from the plan.

Request k is planned for t0 + k * interval before the run starts, and nothing the
service does can move or remove that plan. When the generator is behind, it sends
at once and keeps going until it has caught up, and every request is timed from
the instant it was supposed to leave.
"""

from __future__ import annotations


def run(clock, service, response_hist, service_hist, rate_per_s: int, duration_us: int) -> int:
    """Return requests sent. Fills two histograms from the same run.

    response_hist: completion - intended send time (what a caller waited).
    service_hist:  completion - actual send time (what the naive loop measures).
    """
    interval = 1_000_000 // rate_per_s
    planned = duration_us // interval
    t0 = clock.now()
    for k in range(planned):
        intended = t0 + k * interval
        clock.sleep_until(intended)              # returns at once when behind schedule
        actual = clock.now()
        service.call(clock)
        done = clock.now()
        response_hist.record_value(done - intended)
        service_hist.record_value(done - actual)
    return planned
