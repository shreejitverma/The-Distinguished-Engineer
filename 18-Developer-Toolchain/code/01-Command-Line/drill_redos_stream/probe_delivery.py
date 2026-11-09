#!/usr/bin/env python3
"""Tell real-time delivery from deliver-at-exit on a FIFO.

Usage: probe_delivery.py FIFO

The probe opens the FIFO itself, so the writer's shell redirection blocks until this process is
ready to read: there is no start-up race between the writer and the reader.
It timestamps the first byte and the EOF (the writer closing its end when it exits).
Block buffering holds every record until exit, so the first byte arrives together with EOF;
real-time delivery gets the first byte out about 400 ms before the 500 ms producer exits.
"""
import os
import sys
import time

EARLY_MARGIN_S = 0.200  # first byte must arrive at least this long before EOF to count as early


def probe(fifo: str) -> None:
    fd = os.open(fifo, os.O_RDONLY)  # blocks until the writer opens its end
    first_byte_at = None
    chunks = []
    while True:
        data = os.read(fd, 4096)
        now = time.monotonic()
        if not data:
            eof_at = now
            break
        if first_byte_at is None:
            first_byte_at = now
        chunks.append(data)
    os.close(fd)

    text = b"".join(chunks).decode("ascii", errors="replace")
    total = len([ln for ln in text.splitlines() if ln.strip()])
    early = first_byte_at is not None and (eof_at - first_byte_at) > EARLY_MARGIN_S
    print(f"PIPELINE_DELIVERY_MODE: {'REALTIME_STREAMING' if early else 'BLOCK_BUFFERED_STALL'}")
    print(f"EARLY_ARRIVALS_DURING_PRODUCER_RUN: {early}")
    print(f"TOTAL_RECORDS_RECEIVED: {total}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: probe_delivery.py FIFO", file=sys.stderr)
        sys.exit(2)
    probe(sys.argv[1])
