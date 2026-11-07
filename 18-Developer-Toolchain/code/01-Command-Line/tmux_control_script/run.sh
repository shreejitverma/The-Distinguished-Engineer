#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_LAB="$(mktemp -d)"
SOCK="${TMPDIR_LAB}/tmux_lab.sock"
# The PID fallback matters once step 6 has unlinked the socket: kill-server cannot reach a server whose socket is gone.
trap 'tmux -S "${SOCK}" kill-server 2>/dev/null || true; [ -z "${SERVER_PID:-}" ] || kill "${SERVER_PID}" 2>/dev/null || true; rm -rf "${TMPDIR_LAB}"' EXIT

# 1. Socket Lifecycle and Daemon Startup
# Start an isolated tmux server with an explicit 80x24 geometry and empty config.
# SSH_AUTH_SOCK is set only for the server start so the first pane begins with a known "stale" agent path.
SSH_AUTH_SOCK="agent_v1.sock" tmux -S "${SOCK}" -f /dev/null new-session -d -x 80 -y 24 -s s0 -n win0 "bash --noprofile --norc"

# A -S socket is created with mode 0600 while no client is attached (server.c).
if [ ! -S "${SOCK}" ]; then
    echo "FAIL: ${SOCK} is not a socket" >&2
    exit 1
fi
SOCK_MODE="$(stat -c %a "${SOCK}" 2>/dev/null || stat -f %Lp "${SOCK}")"
echo "1. Socket Lifecycle: stream socket created, mode ${SOCK_MODE}"

SERVER_PID="$(tmux -S "${SOCK}" display-message -p "#{pid}")"
if [ -n "${SERVER_PID}" ] && kill -0 "${SERVER_PID}" 2>/dev/null; then
    echo "   Server daemon: active"
fi

# 2. Session, Window, and Pane PTY Hierarchy
# Window 0 is split horizontally into two panes (0.0 and 0.1)
tmux -S "${SOCK}" split-window -h -t s0:0 "bash --noprofile --norc"
# Window 1 contains a single full-screen pane (1.0)
tmux -S "${SOCK}" new-window -t s0 -n win1 "bash --noprofile --norc"

echo "2. Hierarchy:"
tmux -S "${SOCK}" list-sessions -F "   session #{session_name}: id=#{session_id} windows=#{session_windows}"
tmux -S "${SOCK}" list-windows -t s0 -F "   window #{window_index} (#{window_name}): id=#{window_id} panes=#{window_panes}"
tmux -S "${SOCK}" list-panes -t s0:0 -F "   pane #{pane_index}: id=#{pane_id} size=#{pane_width}x#{pane_height} pty=#{?pane_tty,allocated,none}"
tmux -S "${SOCK}" list-panes -t s0:1 -F "   pane #{pane_index}: id=#{pane_id} size=#{pane_width}x#{pane_height} pty=#{?pane_tty,allocated,none}"

# Each pane must own a distinct character-device slave PTY, not merely report a tty path.
ALL_TTYS="$(tmux -S "${SOCK}" list-panes -s -t s0 -F "#{pane_tty}")"
PANE_COUNT="$(printf '%s\n' "${ALL_TTYS}" | wc -l | tr -d ' ')"
DISTINCT_TTYS="$(printf '%s\n' "${ALL_TTYS}" | sort -u | wc -l | tr -d ' ')"
CHAR_TTYS=0
for tty in ${ALL_TTYS}; do
    if [ -c "${tty}" ]; then CHAR_TTYS=$((CHAR_TTYS + 1)); fi
done
echo "   distinct slave PTYs: ${DISTINCT_TTYS} of ${PANE_COUNT} panes, ${CHAR_TTYS} character devices"

# 3. Headless Automation: send-keys, wait-for, capture-pane
# Pane 0.1 executes work and signals completion over a dedicated tmux channel
tmux -S "${SOCK}" send-keys -t s0:0.1 "echo TASK_COMPLETED; tmux -S \"${SOCK}\" wait-for -S sync_channel" Enter
tmux -S "${SOCK}" wait-for sync_channel

echo "3. Headless Automation:"
echo "   wait-for synchronization: unblocked"
CAPTURED="$(tmux -S "${SOCK}" capture-pane -p -t s0:0.1 | grep -E "^TASK_COMPLETED$" || true)"
echo "   capture-pane output: ${CAPTURED}"

# 4. Environment Propagation and Dynamic Refresh
# Pane 0.0 was created while SSH_AUTH_SOCK was agent_v1.sock, so its shell holds the stale value.
tmux -S "${SOCK}" send-keys -t s0:0.0 "echo \"BEFORE=\$SSH_AUTH_SOCK\"; tmux -S \"${SOCK}\" wait-for -S env_before" Enter
tmux -S "${SOCK}" wait-for env_before

# Simulate a reattach with a new agent: tmux updates its session environment ...
tmux -S "${SOCK}" set-environment -t s0 SSH_AUTH_SOCK "agent_v2.sock"
TMUX_VIEW="$(tmux -S "${SOCK}" show-environment -t s0 SSH_AUTH_SOCK)"

# ... but the running shell keeps its old value until it evaluates the Bourne assignment from show-environment -s.
tmux -S "${SOCK}" send-keys -t s0:0.0 "echo \"UNCHANGED=\$SSH_AUTH_SOCK\"; tmux -S \"${SOCK}\" wait-for -S env_unchanged" Enter
tmux -S "${SOCK}" wait-for env_unchanged
tmux -S "${SOCK}" send-keys -t s0:0.0 "eval \$(tmux -S \"${SOCK}\" show-environment -s -t s0 SSH_AUTH_SOCK); echo \"AFTER=\$SSH_AUTH_SOCK\"; tmux -S \"${SOCK}\" wait-for -S env_done" Enter
tmux -S "${SOCK}" wait-for env_done

PANE_VIEW="$(tmux -S "${SOCK}" capture-pane -p -t s0:0.0)"
echo "4. Environment Propagation:"
echo "   tmux session environment: ${TMUX_VIEW}"
printf '%s\n' "${PANE_VIEW}" | grep -E "^(BEFORE|UNCHANGED|AFTER)=" | sed 's/^/   shell /' || true

# 5. Control Mode (-C) Text Protocol
echo "5. Control Mode Protocol:"
tmux -S "${SOCK}" -C display-message -p "CONTROL_MODE_STREAM_OK" \
    | sed -E "s/%(begin|end) [0-9]+ [0-9]+ 0/%\1 <TIMESTAMP> <CMD_NUM> 0/g"

# 6. Socket Unlink and SIGUSR1 Recreation
PIDS_BEFORE="$(tmux -S "${SOCK}" list-panes -s -t s0 -F "#{pane_pid}" | sort | tr '\n' ' ')"
rm -f "${SOCK}"
SOCKET_GONE="$(test -S "${SOCK}" && echo "still_present" || echo "unlinked")"
echo "6. Resilience and Recovery:"
echo "   socket state after unlink: ${SOCKET_GONE}"

kill -USR1 "${SERVER_PID}"

# Await server recreation of the socket on disk
for i in {1..20}; do
    if [ -S "${SOCK}" ]; then
        break
    fi
    sleep 0.05
done

SOCKET_RESTORED="$(test -S "${SOCK}" && echo "recreated" || echo "failed")"
echo "   socket state after SIGUSR1: ${SOCKET_RESTORED}"

# Verify server state over recreated socket
SURVIVED_WINDOWS="$(tmux -S "${SOCK}" list-windows -t s0 -F "#{window_id}" | wc -l | tr -d ' ')"
echo "   survived windows over restored socket: ${SURVIVED_WINDOWS}"

PIDS_AFTER="$(tmux -S "${SOCK}" list-panes -s -t s0 -F "#{pane_pid}" | sort | tr '\n' ' ')"
if [ -n "${PIDS_BEFORE}" ] && [ "${PIDS_BEFORE}" = "${PIDS_AFTER}" ]; then
    echo "   pane processes after restore: unchanged"
else
    echo "   pane processes after restore: CHANGED"
fi

# Clean shutdown
tmux -S "${SOCK}" kill-server
