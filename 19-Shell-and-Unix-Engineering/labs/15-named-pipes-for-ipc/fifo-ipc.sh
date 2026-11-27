#!/bin/sh
# fifo-ipc: named-pipe (FIFO) behaviors for the note "Named Pipes for IPC".
# Usage: fifo-ipc.sh MODE
# Every mode makes its own scratch directory under ${TMPDIR:-/tmp}, prints key=value lines, kills
# every process it started, and removes the directory. Blocked processes are detected with marker
# files, never with kill -0, because a finished-but-unreaped child still answers kill -0.
#   rendezvous         a writer blocks in open(2) until a reader arrives
#   eof-cycle          EOF reaches a reader when the last writer closes; each session needs a reopen
#   hold-open          exec 3<>FIFO keeps the FIFO from reporting EOF between writers
#   leaked-descriptor  a child that inherits the O_RDWR descriptor delays EOF until it exits
#   competing-readers  two readers share one stream: bytes are split, never copied
#   many-writers       4 writers x 200 lines of 100 bytes arrive intact (needs LAB_PROBE)
#   rpc                request FIFO plus one reply FIFO per client
#   reopen-loss        "while read <FIFO" loses data; one open for the whole loop does not
#   open-order         two processes opening two FIFOs in opposite order deadlock; O_RDWR does not
#   reader-dies        the writer is killed by SIGPIPE when the reader has gone
#   mkfifo-mode        mkfifo honors umask unless -m is given
set -u
# Native zsh does not field split unquoted parameters; the bookkeeping below relies on it.
if [ -n "${ZSH_VERSION-}" ]; then
  emulate -R sh
fi

PIDS=
d=$(mktemp -d "${TMPDIR:-/tmp}/fifo-ipc.XXXXXX") || exit 1

# cleanup: release every FIFO, kill every tracked process, remove the scratch directory.
# A read-write open does not block on Linux or macOS and counts as a writer, so it wakes any
# process left blocked in open(2) on a FIFO, and its close then gives blocked readers an EOF.
cleanup() {
  for p in "$d"/*; do
    if [ -p "$p" ]; then
      { : <>"$p"; } 2>/dev/null
    fi
  done
  for pid in $PIDS; do
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
  done
  rm -rf "$d"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

track() {
  PIDS="$PIDS $1"
}

# wait_file FILE SECONDS: poll for FILE; succeed as soon as it exists.
wait_file() {
  n=$(($2 * 10))
  while [ ! -e "$1" ]; do
    n=$((n - 1))
    [ "$n" -gt 0 ] || return 1
    /bin/sleep 0.1
  done
}

# yesno_kv KEY FILE: print KEY=yes when FILE exists, else KEY=no.
yesno_kv() {
  if [ -e "$2" ]; then echo "$1=yes"; else echo "$1=no"; fi
}

# join_kv KEY FILE: print KEY= and the lines of FILE joined with commas.
join_kv() {
  printf '%s=' "$1"
  paste -s -d, "$2"
}

# cat_kv KEY FILE: print KEY= and the contents of FILE (one line).
cat_kv() {
  printf '%s=' "$1"
  cat "$2"
}

mode_rendezvous() {
  f=$d/fifo
  mkfifo "$f" || exit 1
  {
    printf 'hello\n' >"$f"
    : >"$d/w.done"
  } &
  track $!
  /bin/sleep 1
  yesno_kv writer-finished-without-reader "$d/w.done"
  got=$(cat "$f")
  wait_file "$d/w.done" 5
  yesno_kv writer-finished-after-reader "$d/w.done"
  echo "payload=$got"
}

mode_eof_cycle() {
  f=$d/fifo
  mkfifo "$f" || exit 1
  {
    cat <"$f" >"$d/r1.out"
    : >"$d/r1.done"
  } &
  track $!
  echo a >"$f"
  wait_file "$d/r1.done" 5
  yesno_kv first-reader-saw-eof "$d/r1.done"
  cat_kv first-reader-data "$d/r1.out"
  {
    echo b >"$f"
    : >"$d/w2.done"
  } &
  track $!
  /bin/sleep 1
  yesno_kv second-writer-finished-without-reader "$d/w2.done"
  cat_kv second-reader-data "$f"
}

mode_hold_open() {
  f=$d/fifo
  mkfifo "$f" || exit 1
  exec 3<>"$f"
  {
    cat <"$f" >"$d/r.out"
    : >"$d/r.done"
  } 3>&- &
  track $!
  echo a >"$f"
  echo b >"$f"
  /bin/sleep 1
  yesno_kv reader-saw-eof-while-held "$d/r.done"
  join_kv data-while-held "$d/r.out"
  exec 3>&-
  wait_file "$d/r.done" 5
  yesno_kv reader-saw-eof-after-release "$d/r.done"
}

mode_leaked_descriptor() {
  f=$d/fifo
  mkfifo "$f" || exit 1
  exec 3<>"$f"
  {
    cat <"$f" >"$d/r.out"
    : >"$d/r.done"
  } 3>&- &
  track $!
  # The leaker inherits descriptor 3 and holds it for 3 seconds without touching the FIFO.
  /bin/sleep 3 &
  track $!
  echo a >"$f"
  exec 3>&-
  /bin/sleep 1
  yesno_kv reader-saw-eof-after-parent-closed "$d/r.done"
  join_kv data "$d/r.out"
  wait_file "$d/r.done" 10
  yesno_kv reader-saw-eof-after-leaker-exited "$d/r.done"
}

mode_competing_readers() {
  f=$d/fifo
  mkfifo "$f" || exit 1
  : >"$d/r1.out"
  : >"$d/r2.out"
  cat <"$f" >"$d/r1.out" &
  track $!
  cat <"$f" >"$d/r2.out" &
  track $!
  /bin/sleep 1
  awk 'BEGIN { s = sprintf("%3000s", ""); gsub(/ /, "x", s); printf "%s", s }' >"$f"
  # Wait for the bytes, not for both EOFs: on macOS one of two blocked readers often sees EOF late.
  n=50
  while [ "$n" -gt 0 ]; do
    a=$(wc -c <"$d/r1.out")
    b=$(wc -c <"$d/r2.out")
    [ $((a + b)) -ge 3000 ] && break
    n=$((n - 1))
    /bin/sleep 0.1
  done
  # A copy to the second reader would show up as a total above 3000; give it a second to appear.
  /bin/sleep 1
  a=$(wc -c <"$d/r1.out")
  b=$(wc -c <"$d/r2.out")
  echo "written=3000"
  echo "read-total=$((a + b))"
}

mode_many_writers() {
  : "${LAB_PROBE:?set LAB_PROBE to the compiled fifo-probe}"
  f=$d/fifo
  mkfifo "$f" || exit 1
  exec 3<>"$f"
  {
    cat <"$f" >"$d/all.out"
    : >"$d/r.done"
  } 3>&- &
  track $!
  wpids=
  for c in A B C D; do
    "$LAB_PROBE" write-lines "$f" "$c" 200 100 3>&- &
    track $!
    wpids="$wpids $!"
  done
  for pid in $wpids; do
    wait "$pid"
  done
  exec 3>&-
  wait_file "$d/r.done" 5
  awk '
    length($0) != 99 || $0 !~ /^(A+|B+|C+|D+)$/ { bad++ }
    { n[substr($0, 1, 1)]++ }
    END {
      printf "lines=%d\nbad=%d\n", NR, bad + 0
      printf "A=%d B=%d C=%d D=%d\n", n["A"], n["B"], n["C"], n["D"]
    }' "$d/all.out"
}

# rpc_serve N: serve N requests of the form "ID WORD" from fd 3; reply with WORD upcased.
rpc_serve() {
  left=$1
  while [ "$left" -gt 0 ] && read -r id word <&3; do
    printf '%s\n' "$word" | tr '[:lower:]' '[:upper:]' >"$d/reply.$id"
    left=$((left - 1))
  done
}

# rpc_client ID WORD: create a private reply FIFO, send one request, read the reply.
rpc_client() {
  mkfifo "$d/reply.$1" || return 1
  printf '%s %s\n' "$1" "$2" >"$d/req"
  cat "$d/reply.$1" >"$d/out.$1"
  : >"$d/done.$1"
}

mode_rpc() {
  mkfifo "$d/req" || exit 1
  {
    exec 3<>"$d/req"
    rpc_serve 3
    : >"$d/server.done"
  } &
  track $!
  rpc_client c1 hello &
  track $!
  rpc_client c2 world &
  track $!
  rpc_client c3 fifo &
  track $!
  for id in c1 c2 c3; do
    wait_file "$d/done.$id" 5
    cat_kv "reply.$id" "$d/out.$id"
  done
  wait_file "$d/server.done" 5
  yesno_kv server-exited "$d/server.done"
}

mode_reopen_loss() {
  f=$d/fifo
  mkfifo "$f" || exit 1
  {
    printf 'a\nb\nc\n' >"$f"
  } &
  track $!
  {
    while read -r line <"$f"; do
      echo "$line"
    done >"$d/loop.out"
    : >"$d/loop.done"
  } &
  track $!
  /bin/sleep 1
  yesno_kv reopen-loop-finished "$d/loop.done"
  join_kv reopen-loop-lines "$d/loop.out"
  g=$d/fifo2
  mkfifo "$g" || exit 1
  {
    printf 'a\nb\nc\n' >"$g"
  } &
  track $!
  while read -r line; do
    echo "$line"
  done <"$g" >"$d/single.out"
  join_kv single-open-lines "$d/single.out"
}

mode_open_order() {
  mkfifo "$d/p" "$d/q" "$d/p2" "$d/q2" || exit 1
  {
    exec 3<"$d/p" 4>"$d/q"
    : >"$d/a.done"
  } &
  track $!
  {
    exec 3<"$d/q" 4>"$d/p"
    : >"$d/b.done"
  } &
  track $!
  /bin/sleep 1
  yesno_kv plain-a-finished "$d/a.done"
  yesno_kv plain-b-finished "$d/b.done"
  {
    exec 3<>"$d/p2" 4<>"$d/q2"
    : >"$d/c.done"
  } &
  track $!
  {
    exec 3<>"$d/q2" 4<>"$d/p2"
    : >"$d/e.done"
  } &
  track $!
  wait_file "$d/c.done" 5
  wait_file "$d/e.done" 5
  yesno_kv rdwr-c-finished "$d/c.done"
  yesno_kv rdwr-e-finished "$d/e.done"
}

mode_reader_dies() {
  f=$d/fifo
  mkfifo "$f" || exit 1
  head -n 1 <"$f" >"$d/got" &
  track $!
  {
    exec 3>"$f"
    printf 'one\n' >&3
    /bin/sleep 1
    printf 'two\n' >&3
    : >"$d/survived"
  } 2>/dev/null &
  wpid=$!
  track "$wpid"
  wait "$wpid"
  echo "writer-status=$?"
  yesno_kv writer-survived-second-write "$d/survived"
  cat_kv reader-got "$d/got"
}

mode_mkfifo_mode() {
  umask 077
  mkfifo "$d/m1" || exit 1
  umask 022
  mkfifo "$d/m2" || exit 1
  umask 000
  mkfifo -m 600 "$d/m3" || exit 1
  umask 077
  mkfifo -m 666 "$d/m4" || exit 1
  for n in m1 m2 m3 m4; do
    if [ -p "$d/$n" ]; then kind=fifo; else kind=not-a-fifo; fi
    # shellcheck disable=SC2012 # the symbolic mode string is the datum; find has no portable equivalent
    listing=$(ls -ld "$d/$n")
    printf '%s=%s %.10s\n' "$n" "$kind" "$listing"
  done
}

case ${1-} in
  rendezvous) mode_rendezvous ;;
  eof-cycle) mode_eof_cycle ;;
  hold-open) mode_hold_open ;;
  leaked-descriptor) mode_leaked_descriptor ;;
  competing-readers) mode_competing_readers ;;
  many-writers) mode_many_writers ;;
  rpc) mode_rpc ;;
  reopen-loss) mode_reopen_loss ;;
  open-order) mode_open_order ;;
  reader-dies) mode_reader_dies ;;
  mkfifo-mode) mode_mkfifo_mode ;;
  *)
    echo "usage: fifo-ipc.sh rendezvous|eof-cycle|hold-open|leaked-descriptor|competing-readers|many-writers|rpc|reopen-loss|open-order|reader-dies|mkfifo-mode" >&2
    exit 2
    ;;
esac
