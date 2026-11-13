"""Virtual time and a stalling service for the coordinated omission drill.

All times are integer microseconds. Nothing here reads a real clock, so a run is
deterministic: the generators call clock.now(), clock.sleep_until(), and
service.call() exactly as they would call a monotonic clock, a sleep, and a
blocking request, and only the meaning of time is swapped.
"""

from __future__ import annotations


class VirtualClock:
    """A clock that moves only when a generator sleeps or a request completes."""

    def __init__(self) -> None:
        self._now = 0

    def now(self) -> int:
        return self._now

    def sleep_until(self, deadline: int) -> None:
        """Return at once if the deadline has passed, like a real sleep."""
        if deadline > self._now:
            self._now = deadline

    def advance_to(self, when: int) -> None:
        if when < self._now:
            raise ValueError("virtual time cannot run backwards")
        self._now = when


def service_time_us(sequence: int, low: int, high: int) -> int:
    """Deterministic per-request work in [low, high], a function of the request number only.

    Request k costs the same in every generator, so differences between the reports
    come from the generators and not from the service.
    """
    mixed = (sequence * 2654435761) & 0xFFFFFFFF  # Knuth multiplicative hash
    return low + (mixed >> 7) % (high - low + 1)


class StallingService:
    """One FIFO worker that freezes completely during each stall window.

    A stall is a half-open interval [start, start + length): no request makes
    progress inside it, whether the request was already in service or arrives
    during the stall. This is the shape of a stop-the-world garbage collection
    pause, a blocking fsync on the request thread, or a SIGSTOP.
    """

    def __init__(self, stalls: list[tuple[int, int]], low: int, high: int) -> None:
        self._stalls = sorted(stalls)
        self._low = low
        self._high = high
        self._free_at = 0
        self._served = 0

    def finish_time(self, start: int, work: int) -> int:
        """When `work` microseconds of unstalled time have elapsed after `start`."""
        now = start
        remaining = work
        for stall_start, length in self._stalls:
            stall_end = stall_start + length
            if stall_end <= now:
                continue
            if now < stall_start:
                if now + remaining <= stall_start:
                    return now + remaining
                remaining -= stall_start - now
            now = stall_end
        return now + remaining

    def submit(self, arrival: int) -> int:
        """Queue a request that arrives at `arrival`; return its completion time."""
        work = service_time_us(self._served, self._low, self._high)
        self._served += 1
        start = max(arrival, self._free_at)
        done = self.finish_time(start, work)
        self._free_at = done
        return done

    def call(self, clock: VirtualClock) -> None:
        """A blocking request: returns when the response has arrived."""
        clock.advance_to(self.submit(clock.now()))
