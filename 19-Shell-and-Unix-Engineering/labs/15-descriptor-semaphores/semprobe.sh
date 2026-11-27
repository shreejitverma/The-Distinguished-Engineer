#!/bin/sh
# semprobe.sh MODE DIR [ARG] - one probe per mode; every file and FIFO lives under DIR.
# FIFOTOOL must name the compiled fifotool binary.
#   read1     N=5 tokens, take one with read              -> left=4
#   dd1       N=5 tokens, take one with dd bs=1 count=1   -> left=4
#   plusread  two "+" tokens then a newline, take one with read -> got=++ left=0
#   block     acquire on an empty semaphore blocks until a post wakes it
#   kill      ARG A or B: SIGKILL a token holder, count the tokens that come back
#   inherit   is descriptor 3 visible to an external command, with and without 3>&-
#   eof       read on a pipe whose writers are gone returns immediately with status 1
#   capacity  how many 1-byte tokens a FIFO holds before a write would block
#   jobserver borrow a token from a make-style FIFO jobserver
set -u
here=$(dirname "$0")
# shellcheck source=sem.sh
. "$here/sem.sh"

mode=${1:?usage: semprobe.sh MODE DIR [ARG]}
dir=${2:?usage: semprobe.sh MODE DIR [ARG]}
tool=${FIFOTOOL:?FIFOTOOL must name the fifotool binary}

case $mode in
  read1)
    sem_open "$dir/sem" 5 || exit 1
    sem_wait || exit 1
    left=$("$tool" count "$dir/sem") || exit 1
    printf 'left=%s\n' "$left"
    ;;
  dd1)
    sem_open "$dir/sem" 5 || exit 1
    dd bs=1 count=1 <&3 >/dev/null 2>&1 || exit 1
    left=$("$tool" count "$dir/sem") || exit 1
    printf 'left=%s\n' "$left"
    ;;
  plusread)
    mkfifo -m 600 "$dir/sem" || exit 1
    exec 3<>"$dir/sem" || exit 1
    printf '++\n' >&3 || exit 1
    read -r got <&3 || exit 1
    left=$("$tool" count "$dir/sem") || exit 1
    printf 'got=%s left=%s\n' "$got" "$left"
    ;;
  block)
    sem_open "$dir/sem" 0 || exit 1
    {
      sem_wait && : >"$dir/woke"
    } &
    pid=$!
    trap 'kill "$pid" 2>/dev/null' EXIT
    /bin/sleep 1
    if kill -0 "$pid" 2>/dev/null && [ ! -e "$dir/woke" ]; then
      blocked=yes
    else
      blocked=no
    fi
    sem_post
    wait "$pid"
    wstatus=$?
    if [ -e "$dir/woke" ]; then woke=yes; else woke=no; fi
    printf 'blocked=%s woke=%s status=%s\n' "$blocked" "$woke" "$wstatus"
    ;;
  kill)
    pattern=${3:?usage: semprobe.sh kill DIR A|B}
    sem_open "$dir/sem" 3 || exit 1
    mkfifo -m 600 "$dir/gate" || exit 1
    sem_wait || exit 1
    pidfile=$dir/worker.pid
    case $pattern in
      A) "$here/worker.sh" A "$dir/sem" "$dir/gate" "$pidfile" & ;;
      B) {
        "$here/worker.sh" B "$dir/sem" "$dir/gate" "$pidfile"
        sem_post
      } 2>/dev/null & ;;
      *) exit 2 ;;
    esac
    supervisor=$!
    tries=0
    until [ -s "$pidfile" ]; do
      tries=$((tries + 1))
      [ "$tries" -le 100 ] || {
        kill "$supervisor" 2>/dev/null
        echo "worker never wrote its pid" >&2
        exit 1
      }
      /bin/sleep 0.1
    done
    worker=$(cat "$pidfile") || exit 1
    kill -9 "$worker" || exit 1
    # a shell may print "Killed" for the job it just lost; the notice is not part of the probe output
    { wait "$supervisor"; } 2>/dev/null
    left=$("$tool" count "$dir/sem") || exit 1
    printf 'pattern=%s left=%s\n' "$pattern" "$left"
    ;;
  inherit)
    sem_open "$dir/sem" 1 || exit 1
    plain=$("$tool" isopen 3) || exit 1
    closed=$("$tool" isopen 3 3>&-) || exit 1
    printf 'plain=%s closed=%s\n' "$plain" "$closed"
    ;;
  eof)
    : | {
      # shellcheck disable=SC2034 # the value is unused, only the status matters
      read -r eof_line
      echo "status=$?"
    }
    ;;
  capacity)
    mkfifo -m 600 "$dir/cap" || exit 1
    cap=$("$tool" capacity "$dir/cap") || exit 1
    printf 'capacity=%s\n' "$cap"
    ;;
  jobserver)
    mkfifo -m 600 "$dir/js" || exit 1
    exec 3<>"$dir/js" || exit 1
    printf '++' >&3 || exit 1
    MAKEFLAGS=" -j3 --jobserver-auth=fifo:$dir/js" "$here/borrow.sh"
    ;;
  *)
    echo "semprobe.sh: unknown mode $mode" >&2
    exit 2
    ;;
esac
