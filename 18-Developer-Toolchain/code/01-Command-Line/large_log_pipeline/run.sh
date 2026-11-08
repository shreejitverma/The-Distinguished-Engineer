#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

for tool in python3 zstd pzstd rg jq awk; do
    command -v "${tool}" >/dev/null 2>&1 || { echo "missing required tool: ${tool}" >&2; exit 1; }
done

cd "${WORK_DIR}"

python3 "${SCRIPT_DIR}/generate.py"
cat chunk_0.zst chunk_1.zst chunk_2.zst chunk_3.zst > incident_logs.zst
rm -f chunk_*.zst

decompress_stream() {
    pzstd -q -dc "$1"
}

echo "=== STAGE 1: Zstandard Frame Verification ==="
zstd -l incident_logs.zst | awk 'NR==2 { printf "frames=%d skips=%d check=%s\n", $1, $2, $(NF-1) }'

# Plain concatenated frames: pzstd cannot split them, so it must fall back to serial decompression.
# A pzstd-written archive carries skippable size frames, the precondition for parallel decompression.
zstd -q -dc incident_logs.zst > raw.jsonl
pzstd -q -p 4 -c raw.jsonl > pzstd_written.zst
echo "pzstd_written: $(zstd -l pzstd_written.zst | awk 'NR==2 { print ($2 > 0 ? "skips>0" : "skips=0") }')"
PLAIN_NOTE=$(pzstd -vvv -p 4 -dc incident_logs.zst 2>&1 >/dev/null | grep -c "falling back to serial decompression" || true)
WRITTEN_NOTE=$(pzstd -vvv -p 4 -dc pzstd_written.zst 2>&1 >/dev/null | grep -c "falling back to serial decompression" || true)
echo "serial_fallback: plain=${PLAIN_NOTE} pzstd_written=${WRITTEN_NOTE}"
if [ "$(pzstd -q -dc pzstd_written.zst | cksum)" = "$(zstd -q -dc incident_logs.zst | cksum)" ]; then
    echo "decoded_streams_identical: yes"
else
    echo "decoded_streams_identical: no"
fi

echo "=== STAGE 2: Streaming Decompression (LC_ALL=C) ==="
export LC_ALL=C
echo "byte_order_sort: $(printf 'b\nA\na\nB\n_c\n.d\n' | sort | tr '\n' ' ')"
TOTAL_COUNT=$(decompress_stream incident_logs.zst | wc -l | tr -d " ")
echo "total_records_decompressed: ${TOTAL_COUNT}"

echo "=== STAGE 3: Fast SIMD Pre-Filtering (Ripgrep) ==="
INCIDENT_COUNT=$(decompress_stream incident_logs.zst | rg '"status":\s*5[0-9]{2}' | wc -l | tr -d " ")
echo "filtered_5xx_records: ${INCIDENT_COUNT}"

echo "=== STAGE 4: Streaming Reservoir Sampling (k=5, LCG PRNG) ==="
decompress_stream incident_logs.zst | rg '"status":\s*5[0-9]{2}' | awk '
BEGIN {
    seed = 42;
    k = 5;
    n = 0;
}
function lcg_rand() {
    seed = (seed * 48271) % 2147483647;
    return seed / 2147483647;
}
{
    n++;
    if (n <= k) {
        res[n] = $0;
    } else {
        r = int(lcg_rand() * n) + 1;
        if (r <= k) {
            res[r] = $0;
        }
    }
}
END {
    for (i = 1; i <= k; i++) {
        print res[i];
    }
}' | jq -r '"sample_\(input_line_number): id=\(.id) path=\(.path) status=\(.status) latency=\(.latency_ms)ms error=\(.error)"'

echo "=== STAGE 4b: Reservoir Inclusion Uniformity (k=5, n=100, 4000 trials) ==="
awk '
BEGIN {
    seed = 7; k = 5; n_items = 100; trials = 4000;
    for (t = 1; t <= trials; t++) {
        for (i = 1; i <= k; i++) res[i] = i;
        for (n = k + 1; n <= n_items; n++) {
            seed = (seed * 48271) % 2147483647;
            r = int(seed / 2147483647 * n) + 1;
            if (r <= k) res[r] = n;
        }
        for (i = 1; i <= k; i++) hits[res[i]]++;
    }
    expect = trials * k / n_items;
    lo = 1e9; hi = 0; first_half = 0;
    for (i = 1; i <= n_items; i++) {
        h = hits[i] + 0;
        if (h < lo) lo = h;
        if (h > hi) hi = h;
        if (i <= n_items / 2) first_half += h;
    }
    printf "expected_per_item=%d min=%d max=%d\n", expect, lo, hi;
    printf "first_half_share=%.3f (ideal 0.500)\n", first_half / (trials * k);
    printf "uniform: %s\n", (lo > expect * 0.6 && hi < expect * 1.4 && first_half / (trials * k) > 0.47 && first_half / (trials * k) < 0.53) ? "yes" : "no";
}'

echo "=== STAGE 5: Streaming Latency Histogram & Percentiles ==="
decompress_stream incident_logs.zst | awk -F'"latency_ms": *' '
BEGIN {
    count = 0; sum = 0; min = 999999; max = 0;
    b_limit[1] = 25; b_limit[2] = 50; b_limit[3] = 100; b_limit[4] = 250; b_limit[5] = 500;
    b_name[1] = "   0 -  25 ms";
    b_name[2] = "  25 -  50 ms";
    b_name[3] = "  50 - 100 ms";
    b_name[4] = " 100 - 250 ms";
    b_name[5] = " 250 - 500 ms";
    b_name[6] = "      >=500 ms";
    num_b = 5;
}
NF > 1 {
    split($2, parts, "[,}]");
    val = parts[1] + 0;
    count++;
    sum += val;
    if (val < min) min = val;
    if (val > max) max = val;
    b = num_b + 1;
    for (i = 1; i <= num_b; i++) {
        if (val <= b_limit[i]) { b = i; break; }
    }
    b_count[b]++;
}
END {
    printf "metrics: count=%d min=%.2f mean=%.2f max=%.2f\n", count, min, sum/count, max;
    cum = 0; p50 = ""; p90 = ""; p99 = "";
    for (i = 1; i <= num_b + 1; i++) {
        c = b_count[i] + 0;
        cum += c;
        if (p50 == "" && cum >= count * 0.50) p50 = b_name[i];
        if (p90 == "" && cum >= count * 0.90) p90 = b_name[i];
        if (p99 == "" && cum >= count * 0.99) p99 = b_name[i];
        pct = (c / count) * 100;
        bar_len = int(pct / 2.5);
        bar = "";
        for (j = 0; j < bar_len; j++) bar = bar "#";
        printf "%s | %5d (%5.1f%%) | %s\n", b_name[i], c, pct, bar;
    }
    printf "percentile_est: p50_bucket=\"%s\" p90_bucket=\"%s\" p99_bucket=\"%s\"\n", p50, p90, p99;
}'

echo "=== STAGE 6: Incident Root Cause Frequency Aggregation ==="
decompress_stream incident_logs.zst | rg '"status":\s*5[0-9]{2}' | jq -r '"\(.status) \(.path) \(.error)"' | sort | uniq -c | sort -nr | head -n 3 | awk '{ printf "rank_%d: count=%d status=%s path=%s error=%s\n", NR, $1, $2, $3, $4 }'
