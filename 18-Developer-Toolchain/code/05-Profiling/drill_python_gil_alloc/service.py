#!/usr/bin/env python3
"""The service under test: N worker threads, each scoring batches in a loop.

This is the target for external profilers.  Build first, then run it for as
long as the profiler needs:

    bash build.sh /tmp/recscore-build
    python3 service.py --build-dir /tmp/recscore-build --variant regr --threads 4 --seconds 30

Variants: hold, base (release 1.4), regr (release 1.5), gilfix, fixed (release 1.6).
"""

from __future__ import annotations

import argparse
import importlib
import sys
import threading
import time

REC_SIZE = 64
RECORDS_PER_BATCH = 2000


def make_payload(nrec: int = RECORDS_PER_BATCH) -> bytes:
    """A deterministic batch of nrec 64-byte records."""
    return bytes((i * 131 + 7) % 251 for i in range(nrec * REC_SIZE))


def handle_request(mod, data: bytes, stats: dict) -> int:
    """One request: score one batch.  All native time is spent on this line."""
    return mod.score_batch(data, stats)


def worker(
    mod, data: bytes, stats: dict, deadline: float, done: list[int], slot: int
) -> None:
    batches = 0
    while time.monotonic() < deadline:
        handle_request(mod, data, stats)
        batches += 1
    done[slot] = batches


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--build-dir", required=True, help="directory written by build.sh")
    ap.add_argument(
        "--variant", default="regr", choices=["hold", "base", "regr", "gilfix", "fixed"]
    )
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()

    sys.path.insert(0, args.build_dir)
    mod = importlib.import_module(f"recscore_{args.variant}")
    data = make_payload()
    stats: dict = {}
    done = [0] * args.threads
    deadline = time.monotonic() + args.seconds
    threads = [
        threading.Thread(
            target=worker,
            args=(mod, data, stats, deadline, done, k),
            name=f"worker-{k}",
        )
        for k in range(args.threads)
    ]
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    records = sum(done) * RECORDS_PER_BATCH
    # Throughput varies run to run, so it goes to stderr; the lab never prints it.
    print(
        f"{args.variant}: {args.threads} threads, {records} records, "
        f"{records / elapsed / 1e6:.2f} M records/s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
