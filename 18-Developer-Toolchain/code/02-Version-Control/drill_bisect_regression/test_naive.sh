#!/usr/bin/env bash
# Naive automated bisect test script
# Fails to handle unbuildable commits, resulting in false positive accusations.
set -e

${CC:-cc} -O3 -o order_matcher order_matcher.c
./order_matcher > bench.out

LATENCY=$(awk '/LATENCY_NS:/ {print $2}' bench.out)
if [ "$LATENCY" -gt 10000 ]; then
    exit 1
else
    exit 0
fi
