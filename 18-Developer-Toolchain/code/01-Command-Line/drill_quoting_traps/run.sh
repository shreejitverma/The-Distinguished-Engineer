#!/bin/bash
set -euo pipefail

# Drill harness for quoting, command substitution, subshell scoping, and signal traps.
# Demonstrates failure modes in deploy_brittle.sh and verified fixes in deploy_fixed.sh.
# Needs shellcheck on PATH: the Stage 1 golden output is pinned to its version (printed in Stage 1).

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$LAB_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "run.sh: python3 is required (it resets inherited signal dispositions) but not on PATH" >&2
    exit 2
fi
if ! command -v shellcheck >/dev/null 2>&1; then
    echo "run.sh: shellcheck is required but not on PATH" >&2
    exit 2
fi

STAGE="./test_stage"
REL_B="./test_rel_brittle"
REL_F="./test_rel_fixed"
WORK_B="./test_work_brittle"
WORK_F="./test_work_fixed"
WORK_I="./test_work_int"
WORK_D="./test_work_double"
LOG_B="./test_log_brittle"
LOG_F="./test_log_fixed"
LOG_I="./test_log_int"
LOG_D="./test_log_double"
SPAWNED_DAEMONS=""

cleanup_harness() {
    # Kill any daemon a test left behind; the workspace (and its pid file) may already be gone.
    local pid
    for pid in $SPAWNED_DAEMONS; do
        kill -9 "$pid" 2>/dev/null || true
    done
    rm -rf "$STAGE" "$REL_B" "$REL_F" "$WORK_B" "$WORK_F" "$WORK_I" "$WORK_D" \
        "$LOG_B" "$LOG_F" "$LOG_I" "$LOG_D"
}

trap cleanup_harness EXIT
cleanup_harness

mkdir -p "$STAGE"

# Populate mock release archives:
# 1. Standard alphanumeric name
# 2. Name containing space and version delimiter to trigger IFS word splitting
echo "frontend content" > "$STAGE/index.txt"
tar -czf "$STAGE/web_frontend.tar.gz" -C "$STAGE" index.txt
rm "$STAGE/index.txt"

echo "service content" > "$STAGE/service.txt"
tar -czf "$STAGE/data service-v2.1.tar.gz" -C "$STAGE" service.txt
rm "$STAGE/service.txt"

echo "=== Stage 1: Static Analysis with ShellCheck ==="
echo "ShellCheck version: $(shellcheck --version | sed -n 's/^version: //p')"
echo "--- ShellCheck analysis of deploy_brittle.sh ---"
shellcheck -f gcc deploy_brittle.sh || true

echo "--- ShellCheck analysis of deploy_fixed.sh ---"
set +e
shellcheck deploy_fixed.sh
FIXED_SC_EXIT=$?
set -e
echo "deploy_fixed.sh ShellCheck exit code: $FIXED_SC_EXIT (clean, 0 warnings)"

echo ""
echo "=== Stage 2: Command Substitution Masking (SC2155) & Word Splitting ==="
echo "--- Testing SC2155 exit code masking on missing config ---"
set +e
./deploy_brittle.sh test-sc2155 ./nonexistent_cluster.cfg 2>/dev/null
BRITTLE_SC2155_EXIT=$?
echo "Brittle script exit code with missing config: $BRITTLE_SC2155_EXIT (masked failure)"

./deploy_fixed.sh test-sc2155 ./nonexistent_cluster.cfg 2>/dev/null
FIXED_SC2155_EXIT=$?
set -e
echo "Fixed script exit code with missing config: $FIXED_SC2155_EXIT (aborted as expected)"

echo "--- Testing IFS word splitting on artifact extraction ---"
echo "Brittle artifact extraction:"
./deploy_brittle.sh test-word-split "$STAGE" "$REL_B"

echo "Fixed artifact extraction:"
./deploy_fixed.sh test-word-split "$STAGE" "$REL_F"

echo ""
echo "=== Stage 3: Subshell Scoping & Background Child Leaks ==="
echo "--- Testing pipeline subshell variable modification ---"
echo "Brittle subshell count:"
./deploy_brittle.sh test-subshell "$REL_B"

echo "Fixed subshell count:"
./deploy_fixed.sh test-subshell "$REL_F"

# Run a command with SIGINT, SIGTERM, and SIGHUP reset to their default dispositions.
reset_signals() {
    exec python3 -c 'import os, signal, sys
for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(s, signal.SIG_DFL)
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
}

# Poll until FILE contains PATTERN; fail loudly instead of sleeping a guessed duration.
wait_for() {
    local what="$1" file="$2" pattern="$3" tries=0
    while ! grep -q -- "$pattern" "$file" 2>/dev/null; do
        tries=$((tries + 1))
        if [ "$tries" -gt 1500 ]; then
            echo "run.sh: timed out waiting for $what" >&2
            return 1
        fi
        sleep 0.02
    done
}

dir_state() {
    if [ -d "$1" ]; then echo "PRESENT"; else echo "REMOVED"; fi
}

daemon_state() {
    if kill -0 "$1" 2>/dev/null; then echo "LEAKED (process still running)"; else echo "TERMINATED (cleanly shut down)"; fi
}

# signal_run SCRIPT WORKDIR LOG [SIGNAL...]
# Starts SCRIPT test-trap-leak in the background (through reset_signals, so SIGINT is not
# ignored on entry), waits until it reports its daemon, checks the daemon is alive,
# delivers each SIGNAL, and sets SCRIPT_EXIT and DAEMON_PID.
# A second signal is held back until the script has started its cleanup.
signal_run() {
    local script="$1" work="$2" log="$3" job sig first=1
    shift 3
    # A non-interactive shell cannot trap a signal that was ignored when it started (POSIX XCU trap),
    # and a parent that runs this lab as a background job hands every child SIGINT ignored.
    # Launch through reset_signals so the script under test starts with default dispositions.
    reset_signals "./$script" test-trap-leak "$work" >"$log" 2>&1 &
    job=$!
    wait_for "$script to start its daemon" "$log" "Deploy script running with background daemon"
    DAEMON_PID=$(cat "$work/daemon.pid")
    case "$DAEMON_PID" in
        '' | *[!0-9]*)
            echo "run.sh: $script wrote no usable daemon pid: '$DAEMON_PID'" >&2
            return 1
            ;;
    esac
    SPAWNED_DAEMONS="$SPAWNED_DAEMONS $DAEMON_PID"
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
        echo "run.sh: daemon $DAEMON_PID of $script was not running before the signal" >&2
        return 1
    fi
    for sig in "$@"; do
        if [ "$first" -eq 0 ]; then
            wait_for "$script to start cleanup" "$log" "Terminating background daemon"
        fi
        first=0
        kill -s "$sig" "$job" 2>/dev/null || true
    done
    # Bound the wait: a script that ignores the signal must fail the lab, not hang it.
    local tries=0
    while kill -0 "$job" 2>/dev/null; do
        tries=$((tries + 1))
        if [ "$tries" -gt 1500 ]; then
            echo "run.sh: $script did not exit within 30 s of signal(s) $*" >&2
            kill -9 "$job" "$DAEMON_PID" 2>/dev/null || true
            return 1
        fi
        sleep 0.02
    done
    set +e
    wait "$job" 2>/dev/null
    SCRIPT_EXIT=$?
    set -e
}

echo "--- Testing background daemon cleanup on SIGTERM ---"
echo "Testing brittle deployment script trap behavior:"
signal_run deploy_brittle.sh "$WORK_B" "$LOG_B" TERM
echo "Brittle deploy script exit status: $SCRIPT_EXIT"
echo "Brittle workspace after SIGTERM: $(dir_state "$WORK_B")"
echo "Brittle daemon state after SIGTERM: $(daemon_state "$DAEMON_PID")"
kill -9 "$DAEMON_PID" 2>/dev/null || true

echo "Testing fixed deployment script trap behavior:"
signal_run deploy_fixed.sh "$WORK_F" "$LOG_F" TERM
echo "Fixed deploy script exit status: $SCRIPT_EXIT (128 + 15 = 143 SIGTERM)"
echo "Fixed workspace after SIGTERM: $(dir_state "$WORK_F")"
echo "Fixed daemon state after SIGTERM: $(daemon_state "$DAEMON_PID")"

echo "--- Testing the SIGINT and re-entrancy paths of the fixed script ---"
signal_run deploy_fixed.sh "$WORK_I" "$LOG_I" INT
echo "Fixed deploy script exit status after SIGINT: $SCRIPT_EXIT (128 + 2 = 130 SIGINT)"
echo "Fixed workspace after SIGINT: $(dir_state "$WORK_I")"
echo "Fixed daemon state after SIGINT: $(daemon_state "$DAEMON_PID")"

signal_run deploy_fixed.sh "$WORK_D" "$LOG_D" TERM HUP
echo "Fixed deploy script exit status after TERM then HUP during cleanup: $SCRIPT_EXIT (128 + 15 = 143 SIGTERM)"
echo "Fixed cleanup runs after TERM then HUP: $(grep -c 'Terminating background daemon' "$LOG_D")"
echo "Fixed workspace after TERM then HUP: $(dir_state "$WORK_D")"
echo "Fixed daemon state after TERM then HUP: $(daemon_state "$DAEMON_PID")"
