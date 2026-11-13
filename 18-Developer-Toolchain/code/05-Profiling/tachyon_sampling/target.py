"""Child workload for the out-of-process section: about one second of pure-Python CPU work."""

import time


def busy(deadline):
    s = 0
    while time.perf_counter() < deadline:
        for i in range(1000):
            s += i * i
    return s


if __name__ == "__main__":
    busy(time.perf_counter() + 1.0)
