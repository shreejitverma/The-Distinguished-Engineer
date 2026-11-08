#!/usr/bin/env python3
import re
import sys

PATTERN_VULN = re.compile(
    r"^\[(?P<time>[^\]]+)\]\s+GET\s+path=/api/v1/(?P<route>(([a-zA-Z0-9_-]+)/?)+)\s+status=(?P<status>[0-9]{3})\s+latency=(?P<lat>[0-9]+)ms$"
)

PATTERN_LINEAR = re.compile(
    r"^\[(?P<time>[^\]]+)\]\s+GET\s+path=/api/v1/(?P<route>[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*)/?\s+status=(?P<status>[0-9]{3})\s+latency=(?P<lat>[0-9]+)ms$"
)

def main():
    if len(sys.argv) < 2:
        print("Usage: filter_stream.py --mode=[vulnerable|linear]", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "--mode=vulnerable":
        pattern = PATTERN_VULN
    elif mode == "--mode=linear":
        pattern = PATTERN_LINEAR
    else:
        print(f"Unknown mode: {mode}", file=sys.stderr)
        sys.exit(1)

    total = 0
    valid = 0
    rejected = 0

    for line in sys.stdin:
        line_clean = line.strip()
        if not line_clean:
            continue
        total += 1
        m = pattern.match(line_clean)
        if m:
            valid += 1
            sys.stdout.write(f"[PARSED] status={m.group('status')} route={m.group('route')}\n")
        else:
            rejected += 1
            sys.stdout.write("[REJECTED] malformed_or_unauthorized_route\n")
        sys.stdout.flush()

    sys.stdout.write(f"[SUMMARY] total_processed={total} valid={valid} rejected={rejected}\n")
    sys.stdout.flush()

if __name__ == "__main__":
    main()
