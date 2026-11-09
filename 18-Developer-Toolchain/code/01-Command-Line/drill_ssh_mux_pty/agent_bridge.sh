#!/usr/bin/env bash
# Stale SSH_AUTH_SOCK inside a long-lived tmux pane, and the symlink bridge that fixes it.
# usage: agent_bridge.sh STAGE_DIR   (STAGE_DIR must be short: UNIX socket paths are limited to ~104 bytes)
#
# Every status line printed below is derived from a measured value and asserted;
# a mismatch prints ASSERTION FAILED on stderr and exits 1.
set -euo pipefail

STAGE="${1:?usage: agent_bridge.sh STAGE_DIR}"
LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMUX_SOCK="$STAGE/tmux.sock"
KEY="$STAGE/drill_key"
FAKE_HOME="$STAGE/home"
BRIDGE="$FAKE_HOME/.ssh/ssh_auth_sock"
SESSION=drill
PANE_SEQ=0

fail() { echo "ASSERTION FAILED: $*" >&2; exit 1; }
expect_eq() { [ "$2" = "$3" ] || fail "$1: got '$2', expected '$3'"; }
wait_for() { local n=100; until eval "$1"; do n=$((n - 1)); [ "$n" -gt 0 ] || return 1; sleep 0.05; done; }
yes_no() { if eval "$1"; then echo yes; else echo no; fi; }

cleanup() {
    local f pid
    for f in "$STAGE"/*.pid; do
        [ -f "$f" ] || continue
        pid=$(cat "$f" 2>/dev/null || true)
        [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
    [ -S "$TMUX_SOCK" ] && tmux -S "$TMUX_SOCK" kill-server 2>/dev/null || true
}
trap cleanup EXIT

# start_agent NAME: a real ssh-agent holding one key; sets AGENT_SOCK and AGENT_PID.
start_agent() {
    AGENT_SOCK="$STAGE/$1.sock"
    local out
    out=$(ssh-agent -a "$AGENT_SOCK" -s)
    AGENT_PID=$(printf '%s\n' "$out" | sed -n 's/^SSH_AGENT_PID=\([0-9][0-9]*\);.*/\1/p')
    [ -n "$AGENT_PID" ] || fail "no pid from ssh-agent: $out"
    echo "$AGENT_PID" > "$STAGE/$1.pid"
    SSH_AUTH_SOCK="$AGENT_SOCK" ssh-add -q "$KEY"
}

# stop_agent NAME PID: what sshd does to a forwarded socket when its session closes.
stop_agent() {
    kill "$2"
    wait_for "! kill -0 $2 2>/dev/null" || fail "agent $1 did not exit"
    rm -f "$STAGE/$1.sock" "$STAGE/$1.pid"
}

# pane_keys TEXT: type TEXT plus Enter into the pane's interactive shell.
pane_keys() { tmux -S "$TMUX_SOCK" send-keys -t "$SESSION" "$1" C-m; }

# Helper typed into the pane once: rr N CMD prints "RESULT <exit status> N :<first output line>".
read -r -d '' PANE_HELPER <<'EOF_HELPER' || true
rr() { local o; o=$( { eval "$2"; } 2>&1 ); echo "RESULT $? $1 :${o%%$'\n'*}"; }
EOF_HELPER

# pane_run CMD: run CMD inside the pane's shell; sets PANE_RC and PANE_OUT (first output line).
pane_run() {
    PANE_SEQ=$((PANE_SEQ + 1))
    local want=$((1000 + PANE_SEQ)) line=""
    pane_keys "rr $want '$1'"
    # The typed line starts with "rr", so only the real result line can match ^RESULT.
    wait_for "tmux -S '$TMUX_SOCK' capture-pane -t $SESSION -p -J | grep -q '^RESULT [0-9]* $want :'" \
        || fail "pane command did not finish: $1"
    line=$(tmux -S "$TMUX_SOCK" capture-pane -t "$SESSION" -p -J | grep "^RESULT [0-9]* $want :" | tail -n 1)
    PANE_RC=$(printf '%s\n' "$line" | awk '{print $2}')
    PANE_OUT=${line#*" :"}
}

mkdir -p "$FAKE_HOME/.ssh"
cp "$LAB_DIR/ssh_rc.sh" "$FAKE_HOME/.ssh/rc"
ssh-keygen -q -t ed25519 -N '' -C drill -f "$KEY"

echo "--- 1. Initial Remote Session and tmux Pane Initialization ---"
start_agent a1
A1_SOCK="$AGENT_SOCK"; A1_PID="$AGENT_PID"
# The tmux server and the pane shell inherit SSH_AUTH_SOCK=agent 1, exactly like the first login.
SSH_AUTH_SOCK="$A1_SOCK" tmux -S "$TMUX_SOCK" -f /dev/null new-session -d -x 200 -y 50 \
    -s "$SESSION" -n main -c "$STAGE" "bash --norc --noprofile"
wait_for "tmux -S '$TMUX_SOCK' list-panes -t $SESSION >/dev/null 2>&1" || fail "tmux session did not start"
pane_keys "$PANE_HELPER"
pane_keys 'sleep 600 & BUILD_PID=$!'
pane_run 'echo $SSH_AUTH_SOCK'
expect_eq "pane SSH_AUTH_SOCK" "$PANE_OUT" "$A1_SOCK"
echo "Pane SSH_AUTH_SOCK is agent 1: $(yes_no '[ "$PANE_OUT" = "$A1_SOCK" ]')"
pane_run 'ssh-add -l'
expect_eq "pane ssh-add rc before disconnect" "$PANE_RC" 0
echo "Pane ssh-add -l before disconnect: exit $PANE_RC, key listed: $(yes_no 'printf "%s" "$PANE_OUT" | grep -q ED25519')"

echo ""
echo "--- 2. Client Disconnect and Stale Socket Drift ---"
stop_agent a1 "$A1_PID"
echo "Agent 1 socket after disconnect: exists=$(yes_no '[ -S "$A1_SOCK" ]')"
start_agent a2
A2_SOCK="$AGENT_SOCK"; A2_PID="$AGENT_PID"
# A real tmux client attaches from the new login: update-environment copies SSH_AUTH_SOCK.
if SSH_AUTH_SOCK="$A2_SOCK" python3 "$LAB_DIR/attach_client.py" "$TMUX_SOCK" "$SESSION" SSH_AUTH_SOCK "$A2_SOCK"; then
    ATTACH_OK=yes
else
    ATTACH_OK=no
fi
expect_eq "tmux attach updated the session environment" "$ATTACH_OK" yes
SESSION_VALUE=$(tmux -S "$TMUX_SOCK" show-environment -t "$SESSION" SSH_AUTH_SOCK | cut -d= -f2-)
echo "tmux session environment after real tmux attach is agent 2: $(yes_no '[ "$SESSION_VALUE" = "$A2_SOCK" ]')"
expect_eq "session env" "$SESSION_VALUE" "$A2_SOCK"
pane_run 'echo $SSH_AUTH_SOCK'
expect_eq "pane keeps the stale path" "$PANE_OUT" "$A1_SOCK"
echo "Pane shell still holds agent 1 path: $(yes_no '[ "$PANE_OUT" = "$A1_SOCK" ]')"
pane_run 'ssh-add -l'
expect_eq "stale pane ssh-add rc" "$PANE_RC" 2
echo "Pane ssh-add -l with stale variable (exit $PANE_RC):"
echo "$PANE_OUT"
pane_run 'kill -0 $BUILD_PID && echo alive'
expect_eq "background job" "$PANE_OUT" alive
echo "Long-running background job status: $PANE_OUT"

echo ""
echo "--- 3. In-Place Environment Repair Without Terminating Pane ---"
pane_keys 'export SSH_AUTH_SOCK=$(tmux show-environment SSH_AUTH_SOCK | cut -d= -f2-)'
pane_run 'echo $SSH_AUTH_SOCK'
expect_eq "pane after export" "$PANE_OUT" "$A2_SOCK"
pane_run 'ssh-add -l'
expect_eq "repaired pane ssh-add rc" "$PANE_RC" 0
echo "Pane ssh-add -l after exporting from tmux show-environment: exit $PANE_RC, key listed: $(yes_no 'printf "%s" "$PANE_OUT" | grep -q ED25519')"

echo ""
echo "--- 4. Architectural Paved Road: Persistent Symlink Bridge ---"
# One-time dotfile line: the pane's shell points at the bridge, never at a per-login path.
HOME="$FAKE_HOME" SSH_AUTH_SOCK="$A2_SOCK" sh "$FAKE_HOME/.ssh/rc"
pane_keys "export SSH_AUTH_SOCK='$BRIDGE'"
pane_run 'ssh-add -l'
expect_eq "bridge to agent 2 rc" "$PANE_RC" 0
echo "Pane ssh-add -l via bridge to agent 2: exit $PANE_RC"

stop_agent a2 "$A2_PID"
pane_run 'ssh-add -l'
expect_eq "dangling bridge rc" "$PANE_RC" 2
echo "Pane ssh-add -l via bridge after agent 2 died, before ~/.ssh/rc ran (exit $PANE_RC):"
echo "$PANE_OUT"

start_agent a3
A3_SOCK="$AGENT_SOCK"
HOME="$FAKE_HOME" SSH_AUTH_SOCK="$A3_SOCK" sh "$FAKE_HOME/.ssh/rc"
expect_eq "bridge target" "$(readlink "$BRIDGE")" "$A3_SOCK"
pane_run 'ssh-add -l'
expect_eq "bridge to agent 3 rc" "$PANE_RC" 0
echo "~/.ssh/rc repointed the bridge at agent 3: $(yes_no '[ "$(readlink "$BRIDGE")" = "$A3_SOCK" ]')"
echo "Pane ssh-add -l via bridge after re-attach, no export in the pane: exit $PANE_RC"
