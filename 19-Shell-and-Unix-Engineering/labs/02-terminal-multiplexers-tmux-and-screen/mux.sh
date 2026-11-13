#!/bin/sh
# mux.sh: drive minimux, a miniature terminal multiplexer, with the shell being tested in its pane.
#
# Usage: mux.sh MODE MINIMUX DIR SHELL [SHELL_ARG...]
#   MINIMUX is the compiled minimux binary, DIR a scratch directory (the socket is DIR/s), and
#   SHELL the shell that runs pane.sh inside the pane. MODE is one of:
#     ids        who the pane's shell is: terminal, session leader, foreground group; and the server
#     detach     a client detaches; the pane keeps running and the server keeps its output
#     hangup     detaching sends the pane nothing; killing the server hangs the pane up
#     smallest   one pty, several clients: the size is the minimum of the attached clients
#     latest     one pty, several clients: the size follows the client that typed last
#     winch      a resize reaches the pane as SIGWINCH; attaching at the same size sends none
#     cols       what the pane shell's own COLUMNS and LINES say before and after a resize, and
#                after bash's checkwinsize option is turned on
#     inherited  the same question when the pane inherits COLUMNS=200 and LINES=50 from the
#                environment, as it does after a startup file has exported them
#     broadcast  two attached clients share one pane: both type, both see everything
#     prefix     the server consumes the prefix key: prefix d detaches, prefix twice sends one
#     env        the pane keeps the environment the server had; a later client's never arrives
#     stale      a killed server leaves its socket file behind; kill-server removes it
#     perms      the socket file's mode is the access check
#     sunpath    the socket path limit, and binding relative to the directory to get past it
#     exit       the pane exits: clients learn the status, the socket goes, the server leaves
# Every socket a mode creates is appended to DIR/sockets, so a caller can clean up afterwards with
# "minimux kill-server".
set -u
mode=$1
mux=$2
dir=$3
shift 3
case $0 in
  */*) pane=${0%/*}/pane.sh ;;
  *) pane=./pane.sh ;; # started by bare name from its own directory
esac
sock=$dir/s
policy=smallest
MINIMUX_BIN=$mux
export MINIMUX_BIN

# start_server PANE_MODE SHELL...: start a server on $sock whose pane runs pane.sh PANE_MODE.
start_server() {
  pane_mode=$1
  shift
  printf '%s\n' "$sock" >>"$dir/sockets"
  started=$("$mux" server -p "$policy" "$sock" "$@" "$pane" "$pane_mode" "$dir") || {
    printf '%s\n' "$started"
    return 1
  }
  server_pid=${started#server_pid=}
}

# lines FILE: how many lines FILE has; 0 when it does not exist.
lines() {
  if [ -e "$1" ]; then
    count=$(wc -l <"$1")
    printf '%s\n' "$((count))"
  else
    printf '0\n'
  fi
}

# wait_for_file FILE: wait up to ten seconds for FILE to appear.
wait_for_file() {
  tries=0
  while [ ! -e "$1" ] && [ "$tries" -lt 10 ]; do
    sleep 1
    tries=$((tries + 1))
  done
}

mode_ids() {
  start_server repl "$@" || return 1
  "$mux" drive "$sock" a:attach=24x80 a:expect=ready 'a:send=ids\n' a:expect=stdin_tty= || return 1
  "$mux" info "$sock" server_tty server_leader
  "$mux" kill-server "$sock"
}

mode_detach() {
  start_server repl "$@" || return 1
  before=$("$mux" info "$sock" pane_pid) || return 1
  # 7 bytes: the pane's "ready" line (with CR NL), written before anyone attached.
  # 41 bytes: those 7 plus the 34-byte line the pane writes after client a has gone.
  "$mux" drive "$sock" a:await=detached_bytes=7 a:attach=24x80 a:expect=ready \
    'a:send=hello\n' a:expect=got:hello 'a:send=burst\n' a:close \
    b:await=detached_bytes=41 b:attach=24x80 b:expect=printed-while-detached \
    'b:send=still here\n' 'b:expect=got:still here' b:close b:await=clients=0 || return 1
  after=$("$mux" info "$sock" pane_pid) || return 1
  if [ "$before" = "$after" ] && kill -0 "${after#pane_pid=}" 2>/dev/null; then
    printf 'pane: same process, still running\n'
  else
    printf 'pane: %s then %s\n' "$before" "$after"
  fi
  "$mux" info "$sock" clients detached_bytes
  "$mux" kill-server "$sock"
}

mode_hangup() {
  start_server signals "$@" || return 1
  pane_pid=$("$mux" info "$sock" pane_pid) || return 1
  pane_pid=${pane_pid#pane_pid=}
  "$mux" drive "$sock" a:attach=24x80 a:expect=armed a:close a:await=clients=0 >/dev/null || return 1
  sleep 2 # the pane's shell runs a pending trap within a second; two leaves a margin
  if kill -0 "$pane_pid" 2>/dev/null; then alive=running; else alive=gone; fi
  hups=$(lines "$dir/hup")
  printf 'after the client detached: pane=%s sighup=%s\n' "$alive" "$hups"
  kill -KILL "$server_pid"
  wait_for_file "$dir/hup"
  hups=$(lines "$dir/hup")
  printf 'after the server was killed: sighup=%s\n' "$hups"
}

mode_smallest() {
  start_server repl "$@" || return 1
  "$mux" info "$sock" clients rows cols
  out=$("$mux" drive "$sock" a:attach=40x100 a:expect=ready 'a:send=size\n' a:expect=size: \
    b:attach=24x80 b:expect=ready 'a:send=size\n' a:expect=size: b:close a:await=clients=1 \
    c:attach=50x30 c:expect=ready 'a:send=size\n' a:expect=size: c:close a:await=clients=1 \
    'a:send=size\n' a:expect=size: a:close a:await=clients=0) || return 1
  printf '%s\n' "$out" | grep '^a: '
  "$mux" info "$sock" clients rows cols
  "$mux" kill-server "$sock"
}

mode_latest() {
  policy=latest
  start_server repl "$@" || return 1
  out=$("$mux" drive "$sock" a:attach=40x100 a:expect=ready 'a:send=size\n' a:expect=size: \
    b:attach=24x80 b:expect=size: 'b:send=size\n' b:expect=size: \
    'a:send=size\n' a:expect=size: a:expect=size:) || return 1
  printf '%s\n' "$out" | grep '^a: '
  "$mux" kill-server "$sock"
}

mode_winch() {
  start_server signals "$@" || return 1
  "$mux" drive "$sock" a:attach=24x80 a:expect=armed a:close a:await=clients=0 >/dev/null || return 1
  sleep 2
  count=$(lines "$dir/winch")
  printf 'attached at the size the pane already had: sigwinch=%s\n' "$count"
  "$mux" drive "$sock" b:attach=30x90 b:await=rows=30 b:close >/dev/null || return 1
  wait_for_file "$dir/winch"
  sleep 2
  count=$(lines "$dir/winch")
  printf 'attached at 30x90: sigwinch=%s\n' "$count"
  "$mux" kill-server "$sock"
}

mode_cols() {
  # The pane must inherit neither variable: a shell that never sets them would print the caller's.
  unset COLUMNS LINES
  start_server repl "$@" || return 1
  "$mux" drive "$sock" a:attach=24x80 a:expect=ready 'a:send=cols\n' a:expect=cols: \
    a:resize=30x90 a:await=rows=30 'a:send=size\n' a:expect=size: 'a:send=cols\n' a:expect=cols: \
    'a:send=checkwinsize\n' a:expect=checkwinsize: 'a:send=cols\n' a:expect=cols: || return 1
  "$mux" kill-server "$sock"
}

mode_inherited() {
  COLUMNS=200
  LINES=50
  export COLUMNS LINES # what a startup file that exports them does to every pane started later
  start_server repl "$@" || return 1
  "$mux" drive "$sock" a:attach=24x80 a:expect=ready 'a:send=cols\n' a:expect=cols: \
    a:resize=30x90 a:await=rows=30 'a:send=size\n' a:expect=size: 'a:send=cols\n' a:expect=cols: \
    'a:send=checkwinsize\n' a:expect=checkwinsize: 'a:send=cols\n' a:expect=cols: || return 1
  "$mux" kill-server "$sock"
}

mode_broadcast() {
  start_server repl "$@" || return 1
  "$mux" drive "$sock" a:attach=24x80 b:attach=24x80 a:expect=ready b:expect=ready \
    'a:send=from a\n' 'a:expect=got:from a' 'b:expect=got:from a' \
    'b:send=from b\n' 'a:expect=got:from b' 'b:expect=got:from b' \
    'a:send=quit 0\n' a:closed b:closed
}

mode_prefix() {
  start_server repl "$@" || return 1
  "$mux" drive "$sock" a:attach=24x80 a:expect=ready 'a:send=hex a\002\002b\n' a:expect=hex: \
    'a:send=\002zq\n' a:expect=got: 'a:send=x\002d' a:closed \
    b:attach=24x80 'b:send=y\n' b:expect=got:x || return 1
  "$mux" kill-server "$sock"
}

mode_env() {
  SSH_AUTH_SOCK=/old/agent
  export SSH_AUTH_SOCK
  start_server repl "$@" || return 1
  SSH_AUTH_SOCK=/new/agent
  client=$(env | grep '^SSH_AUTH_SOCK=')
  printf 'client: %s\n' "$client"
  "$mux" drive "$sock" a:attach=24x80 a:expect=ready 'a:send=env\n' a:expect=env: || return 1
  "$mux" kill-server "$sock"
}

mode_stale() {
  start_server repl "$@" || return 1
  state=$("$mux" info "$sock" clients)
  printf 'running: %s\n' "$state"
  kill -KILL "$server_pid"
  tries=0
  state=$("$mux" info "$sock" clients)
  while [ "$state" != connect=ECONNREFUSED ] && [ "$tries" -lt 10 ]; do
    sleep 1
    tries=$((tries + 1))
    state=$("$mux" info "$sock" clients)
  done
  if [ -S "$sock" ]; then file=present; else file=absent; fi
  printf 'server killed with SIGKILL: %s socket-file=%s\n' "$state" "$file"
  start_server repl "$@" || return 1
  state=$("$mux" info "$sock" clients)
  printf 'new server on the stale socket: %s\n' "$state"
  second=$("$mux" server "$sock" "$@" "$pane" repl "$dir")
  printf 'second server on a live socket: %s\n' "$second"
  "$mux" kill-server "$sock"
  state=$("$mux" info "$sock" clients)
  if [ -S "$sock" ]; then file=present; else file=absent; fi
  printf 'after kill-server: %s socket-file=%s\n' "$state" "$file"
}

mode_perms() {
  umask 000
  start_server repl "$@" || return 1
  listing=$(ls -ld "$sock")
  printf 'created under umask 000: %.10s\n' "$listing"
  chmod 000 "$sock"
  state=$("$mux" info "$sock" clients)
  printf 'mode 000: %s\n' "$state"
  chmod 600 "$sock"
  state=$("$mux" info "$sock" clients)
  printf 'mode 600: %s\n' "$state"
  "$mux" kill-server "$sock"
}

mode_sunpath() {
  limit=$("$mux" limits)
  printf '%s\n' "$limit"
  limit=${limit#sun_path=}
  fits=$(awk -v n="$((limit - 1))" 'BEGIN { while (n-- > 0) printf "x"; print "" }')
  sock=$dir/$fits
  start_server repl "$@" || return 1
  state=$("$mux" info "$sock" clients)
  printf 'name one byte shorter than sun_path: %s\n' "$state"
  "$mux" kill-server "$sock"
  sock=$dir/${fits}x
  start_server repl "$@"
  printf 'status=%s\n' "$?"
  part=$(awk 'BEGIN { n = 60; while (n-- > 0) printf "d"; print "" }')
  deep=$dir/$part/$part/$part
  mkdir -p "$deep"
  sock=$deep/s
  start_server repl "$@" || return 1
  if [ "${#sock}" -gt "$limit" ]; then longer=yes; else longer=no; fi
  state=$("$mux" info "$sock" clients)
  printf 'short name in a deep directory: path longer than sun_path=%s %s\n' "$longer" "$state"
  "$mux" kill-server "$sock"
}

mode_exit() {
  start_server repl "$@" || return 1
  "$mux" drive "$sock" a:attach=24x80 a:expect=ready 'a:send=quit 7\n' a:closed || return 1
  state=$("$mux" info "$sock" clients)
  if [ -S "$sock" ]; then file=present; else file=absent; fi
  printf 'afterwards: %s socket-file=%s\n' "$state" "$file"
}

case $mode in
  ids) mode_ids "$@" ;;
  detach) mode_detach "$@" ;;
  hangup) mode_hangup "$@" ;;
  smallest) mode_smallest "$@" ;;
  latest) mode_latest "$@" ;;
  winch) mode_winch "$@" ;;
  cols) mode_cols "$@" ;;
  inherited) mode_inherited "$@" ;;
  broadcast) mode_broadcast "$@" ;;
  prefix) mode_prefix "$@" ;;
  env) mode_env "$@" ;;
  stale) mode_stale "$@" ;;
  perms) mode_perms "$@" ;;
  sunpath) mode_sunpath "$@" ;;
  exit) mode_exit "$@" ;;
  *)
    printf 'mux.sh: unknown mode %s\n' "$mode" >&2
    exit 2
    ;;
esac
