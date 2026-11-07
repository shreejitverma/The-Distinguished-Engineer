#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

export LC_ALL=C

# Stage 6 needs GNU gawk (|& coprocesses); fail loudly instead of skipping it.
if ! command -v gawk >/dev/null 2>&1; then
    echo "error: gawk is required for Stage 6 (brew install gawk, or apt install gawk)" >&2
    exit 3
fi

# 1. Generate test workloads
python3 "${SCRIPT_DIR}/pipeline.py" generate "${WORKDIR}"

# 2. Stage 1: sed hold-space transaction block filter
# Accumulate lines in hold space with 'H'.
# On TX_START, reset hold space to the start line with 'h' (the earlier H is overwritten).
# On TX_END, restore block into pattern space with 'g', and if it contains STATUS 5xx, print with 'p'.
sed -n '/TX_START/,/TX_END/{
    H
    /TX_START/h
    /TX_END/{
        g
        /STATUS 5/p
    }
}' "${WORKDIR}/input_transactions.log" > "${WORKDIR}/out_tx.log"

# 3. Stage 2: sed sliding-window event pair correlator
# Maintain a 2-line sliding window in pattern space using N and D.
# If lines match adjacent REQ and RESP with the same ID, merge them and delete both with d.
# Otherwise print the first line with P and delete it with D (restarting cycle with remaining line).
sed -n \
    -e '$!N' \
    -e '/^REQ id=\([0-9]*\) path=\([^ ]*\)\nRES id=\1 status=\([0-9]*\)$/{' \
    -e 's//PAIR id=\1 path=\2 status=\3/' \
    -e 'p' \
    -e 'd' \
    -e '}' \
    -e 'P' \
    -e 'D' \
    "${WORKDIR}/input_events.log" > "${WORKDIR}/out_events.log"

# 4. Stage 3: sed branching loop to iteratively unwrap nested delimiters
sed \
    -e ':loop' \
    -e 's/\[\([^][]*\)\]/<\1>/' \
    -e 't loop' \
    "${WORKDIR}/input_nested.log" > "${WORKDIR}/out_nested.log"

# 5. Stage 4: awk multiline record parser and associative array aggregator
awk 'BEGIN {
    RS = "---\n"
    FS = "\n"
    OFS = "|"
}
NF > 0 {
    svc = ""; reg = ""; st = 0; lat = 0
    for (i = 1; i <= NF; i++) {
        if ($i ~ /^service=/) { split($i, f, "="); svc = f[2] }
        else if ($i ~ /^region=/) { split($i, f, "="); reg = f[2] }
        else if ($i ~ /^status=/) { split($i, f, "="); st = f[2] + 0 }
        else if ($i ~ /^latency_ms=/) { split($i, f, "="); lat = f[2] + 0 }
    }
    if (svc != "" && reg != "") {
        counts[svc, reg]++
        total_lat[svc, reg] += lat
        if (st >= 500) {
            errors[svc, reg]++
        }
    }
}
END {
    for (k in counts) {
        split(k, parts, SUBSEP)
        s = parts[1]
        r = parts[2]
        c = counts[k]
        e = (k in errors) ? errors[k] : 0
        avg = total_lat[k] / c
        printf "%s|%s|%d|%d|%.1f\n", s, r, c, e, avg
    }
}' "${WORKDIR}/input_records.log" > "${WORKDIR}/out_awk_metrics.log"

# 6. Stage 5: awk SUBSEP internal representation verification
# Find the byte value of SUBSEP by comparing it with sprintf("%c", n) for every n, instead of assuming it.
awk 'BEGIN {
    a["test", "subsep"] = 1
    code = 0
    for (n = 1; n < 256; n++) {
        if (sprintf("%c", n) == SUBSEP) { code = n }
    }
    for (k in a) {
        printf "key_len=%d subsep_len=%d subsep_idx=%d subsep_hex=%02x\n", length(k), length(SUBSEP), index(k, SUBSEP), code
    }
}' > "${WORKDIR}/out_subsep.log"

# 7. Stage 6: gawk coprocess two-way I/O with half-duplex closure
# closeto=1 sends EOF to the child with close(cmd, "to"); closeto=0 leaves the write end open.
cat > "${WORKDIR}/coproc.awk" <<'AWK'
BEGIN {
    cmd = "sort -t\"|\" -k4,4nr -k5,5nr"
    while ((getline line < ARGV[1]) > 0) {
        print line |& cmd
        sent++
    }
    if (sent == 0) {
        print "error: no input for the coprocess" > "/dev/stderr"
        exit 1
    }
    if (closeto) {
        close(cmd, "to")
    }
    while ((cmd |& getline sorted_line) > 0) {
        print sorted_line
    }
    close(cmd)
}
AWK
gawk -v closeto=1 -f "${WORKDIR}/coproc.awk" "${WORKDIR}/out_awk_metrics.log" </dev/null > "${WORKDIR}/out_coproc.log"

# Same program without close(cmd, "to"): sort never sees EOF, so the read loop blocks until the deadline kills it.
python3 "${SCRIPT_DIR}/pipeline.py" deadline 3 "${WORKDIR}/out_nohalfclose.log" -- \
    gawk -v closeto=0 -f "${WORKDIR}/coproc.awk" "${WORKDIR}/out_awk_metrics.log"

echo "--- Top error rates by service and region (gawk coprocess sort) ---"
while IFS="|" read -r svc reg count err avg; do
    printf "%s|%s: count=%s errors=%s avg_latency=%sms\n" "${svc}" "${reg}" "${count}" "${err}" "${avg}"
done < "${WORKDIR}/out_coproc.log"

# 8. Run reference verifier and print results
python3 "${SCRIPT_DIR}/pipeline.py" verify "${WORKDIR}"
