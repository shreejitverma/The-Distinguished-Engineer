#!/usr/bin/env bash
# Drill: SSH multiplexing, agent forwarding drift, and broken PTY recovery.
# Tools needed on PATH (not a harness requirement): ssh ssh-agent ssh-add ssh-keygen tmux stty ps python3 bash.
# Every status line is derived from a measured result and asserted; a mismatch exits 1.
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$LAB_DIR"

for tool in ssh ssh-agent ssh-add ssh-keygen tmux stty ps python3 bash; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

# A short path: UNIX socket paths are limited to 104 bytes on macOS and 108 on Linux.
STAGE="$(mktemp -d /tmp/dsmp.XXXXXX)"

fail() { echo "ASSERTION FAILED: $*" >&2; exit 1; }
expect_eq() { [ "$2" = "$3" ] || fail "$1: got '$2', expected '$3'"; }
wait_for() { local n=100; until eval "$1"; do n=$((n - 1)); [ "$n" -gt 0 ] || return 1; sleep 0.05; done; }
yes_no() { if eval "$1"; then echo yes; else echo no; fi; }
quiet_path() { sed "s#$STAGE#<stage>#g"; }

cleanup() {
    local f pid
    for f in "$STAGE"/*.pid; do
        [ -f "$f" ] || continue
        pid=$(cat "$f" 2>/dev/null || true)
        [ -n "$pid" ] && kill -KILL "$pid" 2>/dev/null || true
    done
    rm -rf "$STAGE"
}
trap cleanup EXIT

# start_master NAME MODE: launch the mock master; sets MASTER_SOCK, MASTER_PID, MASTER_JOB.
start_master() {
    MASTER_SOCK="$STAGE/$1.sock"
    python3 mux_server.py --socket "$MASTER_SOCK" --mode "$2" --pid-file "$STAGE/$1.pid" &
    MASTER_JOB=$!
    wait_for "[ -s '$STAGE/$1.pid' ]" || fail "mock master $1 did not start"
    MASTER_PID=$(cat "$STAGE/$1.pid")
    expect_eq "$1 pid file" "$MASTER_PID" "$MASTER_JOB"
}

# proc_state PID: first letter of the ps state column.
proc_state() { ps -o stat= -p "$1" | tr -d ' ' | cut -c1; }

echo "=== Stage 1: OpenSSH ControlMaster Lockup and Multiplex Diagnostics ==="
echo "--- 1.1 Dead Master Socket Detection ---"
DEAD_SOCK="$STAGE/dead_control.sock"
python3 -c 'import socket, sys; s = socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.close()' "$DEAD_SOCK"
set +e
OUT=$(ssh -S "$DEAD_SOCK" -O check dummy 2>&1)
RC=$?
set -e
OUT=${OUT//$'\r'/}
echo "$OUT" | quiet_path
expect_eq "dead master exit code" "$RC" 255
printf '%s' "$OUT" | grep -q "Connection refused" || fail "dead master message: $OUT"
echo "Dead master check exit code: $RC (Connection refused)"
echo "Orphan socket inode still on disk: $(yes_no '[ -S "$DEAD_SOCK" ]')"
rm -f "$DEAD_SOCK"

echo ""
echo "--- 1.2 Stopped Master Socket Stall & Recovery ---"
start_master stopped stopped
STOPPED_SOCK="$MASTER_SOCK"; STOPPED_PID="$MASTER_PID"; STOPPED_JOB="$MASTER_JOB"
wait_for "[ \"\$(proc_state $STOPPED_PID)\" = T ]" || fail "master never reached state T"
echo "Master process state: $(proc_state "$STOPPED_PID") (stopped by SIGSTOP)"

set +e
python3 -c '
import subprocess, sys
try:
    subprocess.run(["ssh", "-S", sys.argv[1], "-O", "check", "dummy"], timeout=0.5, capture_output=True, check=False)
except subprocess.TimeoutExpired:
    sys.exit(124)
sys.exit(0)
' "$STOPPED_SOCK"
RC=$?
set -e
expect_eq "stopped master check status" "$RC" 124
echo "Stopped master alive check: TIMED OUT after 0.5s (multiplex client wedged), status $RC"

kill -TERM "$STOPPED_PID"
sleep 0.3
expect_eq "state after SIGTERM" "$(proc_state "$STOPPED_PID")" T
echo "After kill -TERM: state $(proc_state "$STOPPED_PID") (SIGTERM stays pending while the process is stopped)"
kill -KILL "$STOPPED_PID"
{ wait "$STOPPED_JOB" || true; } 2>/dev/null
echo "After kill -KILL: process gone: $(yes_no '! kill -0 "$STOPPED_PID" 2>/dev/null')"
echo "Orphan socket inode after the kill: $(yes_no '[ -S "$STOPPED_SOCK" ]')"
set +e
OUT=$(ssh -S "$STOPPED_SOCK" -O check dummy 2>&1)
RC=$?
set -e
OUT=${OUT//$'\r'/}
expect_eq "check after kill" "$RC" 255
printf '%s' "$OUT" | grep -q "Connection refused" || fail "check after kill: $OUT"
echo "Alive check after the kill: exit $RC (Connection refused)"
rm -f "$STOPPED_SOCK" "$STAGE/stopped.pid"

echo ""
echo "--- 1.3 Active OpenSSH PROTOCOL.mux Communication ---"
start_master live live
LIVE_SOCK="$MASTER_SOCK"; LIVE_PID="$MASTER_PID"; LIVE_JOB="$MASTER_JOB"
OUT=$(ssh -S "$LIVE_SOCK" -O check dummy 2>&1)
OUT=${OUT//$'\r'/}
REPORTED=$(printf '%s' "$OUT" | sed -n 's/^Master running (pid=\([0-9][0-9]*\))$/\1/p')
expect_eq "pid reported by -O check" "$REPORTED" "$LIVE_PID"
echo "Master running (pid=PID)"
echo "Reported pid equals the master process pid: $(yes_no '[ "$REPORTED" = "$LIVE_PID" ]')"
OUT=$(ssh -S "$LIVE_SOCK" -O exit dummy 2>&1)
OUT=${OUT//$'\r'/}
echo "$OUT"
expect_eq "-O exit message" "$OUT" "Exit request sent."
{ wait "$LIVE_JOB" || true; } 2>/dev/null
echo "Master process gone after -O exit: $(yes_no '! kill -0 "$LIVE_PID" 2>/dev/null')"
echo "Control socket removed after -O exit: $(yes_no '[ ! -e "$LIVE_SOCK" ]')"
rm -f "$STAGE/live.pid"

echo ""
echo "--- 1.4 Stop Listening Versus Exit ---"
start_master drain live
DRAIN_SOCK="$MASTER_SOCK"; DRAIN_PID="$MASTER_PID"; DRAIN_JOB="$MASTER_JOB"
OUT=$(ssh -S "$DRAIN_SOCK" -O stop dummy 2>&1)
OUT=${OUT//$'\r'/}
echo "$OUT"
expect_eq "-O stop message" "$OUT" "Stop listening request sent."
wait_for "[ ! -e '$DRAIN_SOCK' ]" || fail "socket still present after -O stop"
echo "Control socket removed after -O stop: $(yes_no '[ ! -e "$DRAIN_SOCK" ]')"
echo "Master process still running after -O stop: $(yes_no 'kill -0 "$DRAIN_PID" 2>/dev/null')"
expect_eq "master alive after stop" "$(yes_no 'kill -0 "$DRAIN_PID" 2>/dev/null')" yes
kill -TERM "$DRAIN_PID"
{ wait "$DRAIN_JOB" || true; } 2>/dev/null

echo ""
echo "=== Stage 2: Stale SSH_AUTH_SOCK in Reattached tmux Session ==="
mkdir -p "$STAGE/agent_drill"
bash agent_bridge.sh "$STAGE/agent_drill"

echo ""
echo "=== Stage 3: Corrupted PTY Line Discipline and Blind Recovery ==="
python3 pty_drill.py
