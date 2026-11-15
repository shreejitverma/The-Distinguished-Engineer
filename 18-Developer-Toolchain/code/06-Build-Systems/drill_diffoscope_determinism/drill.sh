#!/usr/bin/env bash
# Runs inside the lab container. For each stage, builds the same source on two
# "CI agents" (different checkout paths, different start times, different hash seeds),
# diffs the release tarballs with diffoscope, and summarizes what differs.
set -euo pipefail

LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_A=/work/agent-a/repo
ROOT_B=/work/agent-b/checkouts/repo
REPORTS="$(mktemp -d)"
trap 'rm -rf "${REPORTS}"' EXIT

# The Python generator's set order depends on the per-process str hash seed. Real CI gets a
# random seed per process; the lab pins two different seeds so the two agents always disagree.
SEED_A=1
SEED_B=2

python3 "${LAB}/summarize.py" toolchain

build_on() {  # build_on ROOT SEED FIXES
    rm -rf "$1"
    mkdir -p "$1"
    cp -R "${LAB}/src" "$1/src"
    PYTHONHASHSEED="$2" bash "$1/src/build.sh" "$1" "$3" >/dev/null 2>&1 \
        || { echo "build failed in $1 with FIXES='$3'" >&2; PYTHONHASHSEED="$2" bash -x "$1/src/build.sh" "$1" "$3" >&2; exit 1; }
}

stage() {  # stage N NAME FIXES
    local n="$1" name="$2" fixes="$3" rc=0
    mkdir -p "${ROOT_A%/repo}" "${ROOT_B%/repo}"
    build_on "${ROOT_A}" "${SEED_A}" "${fixes}"
    # Agent B starts at least two seconds later, so any clock read lands on a different second.
    sleep 2
    build_on "${ROOT_B}" "${SEED_B}" "${fixes}"
    diffoscope --no-progress --json "${REPORTS}/stage${n}.json" \
        "${ROOT_A}/dist/engine.tar.gz" "${ROOT_B}/dist/engine.tar.gz" >/dev/null 2>"${REPORTS}/stage${n}.err" || rc=$?
    if [[ "${rc}" -gt 1 ]]; then
        echo "diffoscope failed with exit ${rc}" >&2
        cat "${REPORTS}/stage${n}.err" >&2
        exit 1
    fi
    if [[ -n "${DRILL_KEEP:-}" ]]; then
        cp "${REPORTS}/stage${n}.json" "${DRILL_KEEP}/" 2>/dev/null || true
    fi
    python3 "${LAB}/summarize.py" stage "${n}" "${name}" "${fixes}" "${rc}" \
        "${REPORTS}/stage${n}.json" "${ROOT_A}" "${ROOT_B}"
}

stage 0 "as first written" ""
stage 1 "pin clocks to SOURCE_DATE_EPOCH" "epoch"
stage 2 "map build paths out" "epoch,prefix"
stage 3 "sort generator and archive order" "epoch,prefix,sort"
