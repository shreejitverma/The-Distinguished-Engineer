#!/usr/bin/env bash
# Robust automated bisect test script
# Employs exit code 125 for untestable commits and validates benchmark latency SLA.

# Step 1: Clean build artifacts
rm -f order_matcher bench.out

# Step 2: Attempt compilation. Exit 125 marks the revision as untestable/skip.
${CC:-cc} -O3 -o order_matcher order_matcher.c 2>/dev/null || exit 125

# Step 3: Run benchmark. If binary crashes or runner fails, mark untestable.
./order_matcher > bench.out 2>/dev/null || exit 125

# Step 4: Extract measured latency and evaluate against the 10,000 ns (10.0 us) SLA.
LATENCY=$(awk '/LATENCY_NS:/ {print $2}' bench.out)
[ -n "$LATENCY" ] || exit 125

if [ "$LATENCY" -gt 10000 ]; then
    exit 1  # Performance regression detected (BAD)
else
    exit 0  # Within latency SLA (GOOD)
fi
