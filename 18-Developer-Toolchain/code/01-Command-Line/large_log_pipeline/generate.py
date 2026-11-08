#!/usr/bin/env python3
"""Generate deterministic synthetic multi-frame compressed log data for lab triage."""

import json
import random
import subprocess
import sys


def main() -> None:
    random.seed(1337)
    endpoints = ["/api/v1/checkout", "/api/v1/cart", "/api/v1/search", "/api/v1/auth", "/healthz"]
    statuses = [200, 200, 200, 200, 200, 200, 200, 201, 204, 400, 401, 404, 500, 502, 503, 504]
    status_weights = [70, 70, 70, 70, 70, 70, 70, 10, 5, 5, 2, 3, 1, 1, 1, 1]

    total_records = 25000
    num_chunks = 4
    chunk_size = total_records // num_chunks

    for chunk in range(num_chunks):
        lines = []
        start_id = chunk * chunk_size
        end_id = (chunk + 1) * chunk_size
        for i in range(start_id, end_id):
            status = random.choices(statuses, weights=status_weights)[0]
            path = random.choice(endpoints)
            if status >= 500:
                lat = random.expovariate(1.0 / 450.0) + 100.0
                err = random.choice(["database_timeout", "connection_refused", "oom_worker_killed"])
            elif status >= 400:
                lat = random.expovariate(1.0 / 25.0) + 5.0
                err = "client_error"
            else:
                lat = random.expovariate(1.0 / 15.0) + 1.0
                err = "none"
            rec = {
                "id": i + 1,
                "method": "POST" if path in ["/api/v1/checkout", "/api/v1/cart", "/api/v1/auth"] else "GET",
                "path": path,
                "status": status,
                "latency_ms": round(lat, 2),
                "error": err
            }
            lines.append(json.dumps(rec) + "\n")

        chunk_filename = f"chunk_{chunk}.zst"
        with open(chunk_filename, "wb") as out_f:
            proc = subprocess.Popen(["zstd", "-q", "-c"], stdin=subprocess.PIPE, stdout=out_f)
            proc.communicate("".join(lines).encode("utf-8"))
            if proc.returncode != 0:
                sys.exit(f"Failed to compress chunk {chunk}")


if __name__ == "__main__":
    main()
