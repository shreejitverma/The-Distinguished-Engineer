#!/bin/sh
# service.sh: a minimal job-processing service that answers SIGTERM and SIGINT four ways.
# Usage: service.sh MODE WORKDIR UNIT [MAX_JOBS]
#   MODE      drain | abort | foreground | stubborn
#   WORKDIR   existing directory; the service appends to WORKDIR/log and creates WORKDIR/ready
#   UNIT      seconds one job takes (a /bin/sleep child stands in for the work)
#   MAX_JOBS  stop by itself after this many jobs (default 1000)
# Modes:
#   drain       first signal: stop taking jobs, let the in-flight job finish, exit 0;
#               second signal: kill the in-flight job, exit 3
#   abort       first signal: kill the in-flight job at once, exit 143
#   foreground  like drain, but the job runs in the foreground, so POSIX defers the trap
#   stubborn    logs the signal and carries on (only SIGKILL stops it)
# Log lines: start N, term-received, force-requested, term-ignored, done N ok|killed,
#            exit drained|forced|aborted|complete N
if [ "$#" -lt 3 ]; then
  echo "usage: service.sh MODE WORKDIR UNIT [MAX_JOBS]" >&2
  exit 2
fi
mode=$1
work=$2
unit=$3
max=${4:-1000}

stopping=0
forcing=0
n=0
child=
rc=0
last=0

log() {
  printf '%s\n' "$*" >>"$work/log"
}

# drain and foreground: the handler only records the request; the main loop acts on it.
# shellcheck disable=SC2329 # invoked through trap
on_signal_drain() {
  if [ "$stopping" -eq 0 ]; then
    stopping=1
    log term-received
  else
    forcing=1
    log force-requested
  fi
}

# abort: the handler stops the in-flight job and leaves at once.
# shellcheck disable=SC2329 # invoked through trap
on_signal_abort() {
  log term-received
  if [ -n "$child" ]; then
    kill "$child" 2>/dev/null
    wait "$child" 2>/dev/null
  fi
  log "exit aborted $n"
  exit 143
}

# shellcheck disable=SC2329 # invoked through trap
on_signal_stubborn() {
  log term-ignored
}

# wait_job: wait for $child to end, surviving signals that interrupt the wait, and set rc to its status.
# A trapped signal makes wait return a status above 128 while the child still runs (POSIX.1-2024
# XCU 2.12), and a child killed by a signal gives the same status, so only kill -0 tells whether the
# child is gone. The second signal of drain mode kills the child here, once. When the loop ends,
# the last status may be the interrupted wait's (ksh93 returns 1) because the child died in the same
# instant; the shell still remembers the child's own status, so one more wait collects it, and a
# 127 there means the loop's own wait already consumed it.
wait_job() {
  rc=
  while kill -0 "$child" 2>/dev/null; do
    if [ "$forcing" -eq 1 ]; then
      forcing=2
      kill "$child" 2>/dev/null
    fi
    wait "$child"
    rc=$?
  done
  wait "$child" 2>/dev/null
  last=$?
  if [ "$last" -ne 127 ]; then
    rc=$last
  fi
  rc=${rc:-127}
}

case $mode in
  drain | foreground) trap on_signal_drain TERM INT ;;
  abort) trap on_signal_abort TERM INT ;;
  stubborn) trap on_signal_stubborn TERM INT ;;
  *)
    echo "service.sh: unknown mode $mode" >&2
    exit 2
    ;;
esac

while [ "$stopping" -eq 0 ] && [ "$n" -lt "$max" ]; do
  n=$((n + 1))
  log "start $n"
  [ -e "$work/ready" ] || : >"$work/ready"
  if [ "$mode" = foreground ]; then
    /bin/sleep "$unit"
    rc=$?
  else
    /bin/sleep "$unit" &
    child=$!
    wait_job
  fi
  child=
  if [ "$rc" -gt 128 ]; then
    log "done $n killed"
  else
    log "done $n ok"
  fi
done

if [ "$forcing" -ne 0 ]; then
  log "exit forced $n"
  exit 3
fi
if [ "$stopping" -ne 0 ]; then
  log "exit drained $n"
  exit 0
fi
log "exit complete $n"
exit 0
