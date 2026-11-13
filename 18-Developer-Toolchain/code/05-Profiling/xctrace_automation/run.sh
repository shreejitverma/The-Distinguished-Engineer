#!/usr/bin/env bash
# requires: macos
#
# Record one workload with xctrace (Time Profiler, CPU Profiler, and two option variants), export
# the traces to XML, and print facts that hold on every run.
# Needs Xcode (xctrace, otool); no root, no network.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export LC_ALL=C

ROUNDS=120
SHORT_ROUNDS=20
TABLE='/trace-toc/run[@number="1"]/data/table'

# Silent fault injection: prints nothing, fails the lab if the XML reader accepts a broken export.
python3 "${SCRIPT_DIR}/selftest.py"

"${CC:-cc}" -O1 -g -fno-omit-frame-pointer -o "${WORK}/workload" "${SCRIPT_DIR}/workload.c"

# xctrace narrates on stdout and reports lost events as a run issue, so its output goes to a
# log that is shown only when a step fails.
quiet() {
    if ! "$@" >"${WORK}/step.log" 2>&1; then
        cat "${WORK}/step.log" >&2
        echo "failed: $*" >&2
        exit 1
    fi
}

# record <template> <trace> <rounds> [extra xctrace record options...]
record() {
    local template="$1" trace="$2" rounds="$3"
    shift 3
    quiet xcrun xctrace record --template "${template}" --output "${trace}" --no-prompt "$@" \
        --target-stdout /dev/null --launch -- "${WORK}/workload" "${rounds}"
}

export_toc() {
    quiet xcrun xctrace export --input "$1" --toc --output "$2"
}

export_table() {
    local trace="$1" predicate="$2" out="$3"
    quiet xcrun xctrace export --input "${trace}" --xpath "${TABLE}[${predicate}]" --output "${out}"
}

record 'Time Profiler' "${WORK}/tp.trace" "${ROUNDS}"
export_toc "${WORK}/tp.trace" "${WORK}/tp-toc.xml"
export_table "${WORK}/tp.trace" '@schema="time-profile"' "${WORK}/tp.xml"
export_table "${WORK}/tp.trace" '@schema="os-signpost" and @category="PointsOfInterest"' "${WORK}/tp-signpost.xml"
xcrun xctrace show "${WORK}/tp.trace" --track 'Time Profiler' --calltree 'Profile' \
    --hide-heaviest-stack >"${WORK}/tp-show.txt" 2>"${WORK}/step.log" || {
    cat "${WORK}/step.log" >&2
    exit 1
}

record 'CPU Profiler' "${WORK}/cp.trace" "${ROUNDS}"
export_toc "${WORK}/cp.trace" "${WORK}/cp-toc.xml"
export_table "${WORK}/cp.trace" '@schema="cpu-profile"' "${WORK}/cp.xml"

# Recording options: print the template's options as JSON, then write three variants of them.
xcrun xctrace record --template 'Time Profiler' --show-recording-options \
    >"${WORK}/options.json" 2>"${WORK}/step.log" || {
    cat "${WORK}/step.log" >&2
    exit 1
}
python3 "${SCRIPT_DIR}/options.py" "${WORK}"

# A file that holds only the changed key must be refused before any recording starts.
if xcrun xctrace record --template 'Time Profiler' --output "${WORK}/partial.trace" --no-prompt \
    --recording-options "${WORK}/options-partial.json" --target-stdout /dev/null \
    --launch -- "${WORK}/workload" 1 >"${WORK}/partial.log" 2>&1; then
    echo accepted >"${WORK}/partial.status"
else
    echo rejected >"${WORK}/partial.status"
fi

record 'Time Profiler' "${WORK}/hf.trace" "${SHORT_ROUNDS}" --recording-options "${WORK}/options-high-frequency.json"
export_toc "${WORK}/hf.trace" "${WORK}/hf-toc.xml"
export_table "${WORK}/hf.trace" '@schema="time-profile"' "${WORK}/hf.xml"

record 'Time Profiler' "${WORK}/wt.trace" "${SHORT_ROUNDS}" --recording-options "${WORK}/options-waiting-threads.json"
export_toc "${WORK}/wt.trace" "${WORK}/wt-toc.xml"

python3 "${SCRIPT_DIR}/report.py" "${WORK}"
