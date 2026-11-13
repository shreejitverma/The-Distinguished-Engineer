#!/bin/sh
# script.sh MODE: what fg, bg and jobs do in a shell that is not interactive.
#   fg         `set -m` in a script on a terminal, then fg: does the job get the terminal?
#   fg_stderr  the same with the standard error of fg redirected away from the terminal
#   nojc       no terminal and no `set -m`: job control is disabled
# probe.sh gives the first two modes a pseudo-terminal; the lab runs nojc directly.
# Needs FGPG (the compiled fgpg.c) and LOGDIR (an empty scratch directory) in the environment.
set -u
mode=${1:?usage: script.sh MODE}

log() {
  printf '%s\n' "$*" >>"$LOGDIR/script.log"
}

# wait_file FILE SECONDS: succeed once FILE exists, fail after SECONDS.
wait_file() {
  wf_tries=$(($2 * 10))
  while [ ! -e "$1" ]; do
    if [ "$wf_tries" -le 0 ]; then
      return 1
    fi
    "$FGPG" msleep 100
    wf_tries=$((wf_tries - 1))
  done
  return 0
}

# end_job NAME: kill the helper NAME if it is still there, quietly.
# By process ID, never by group: without job control the helper shares this script's process group.
end_job() {
  if [ -e "$LOGDIR/$1.pid" ]; then
    read -r ej_pid _ <"$LOGDIR/$1.pid"
    { kill -s KILL "$ej_pid"; } 2>/dev/null
  fi
  { wait; } 2>/dev/null
}

case $mode in
  fg | fg_stderr)
    set -m
    # The job gives up after 3 s if nobody hands it the terminal, so fg cannot hang the script.
    "$FGPG" job "$LOGDIR" a bg 7 3000 &
    wait_file "$LOGDIR/a.ready" 8 || log a_not_ready
    if [ "$mode" = fg ]; then
      fg >"$LOGDIR/fg.out"
    else
      fg >"$LOGDIR/fg.out" 2>"$LOGDIR/fg.err"
    fi
    log "fg_status=$?"
    end_job a
    ;;
  nojc)
    "$FGPG" hold "$LOGDIR" h &
    pid=$!
    wait_file "$LOGDIR/h.ready" 8 || log h_not_ready
    fg >"$LOGDIR/fg.out" 2>"$LOGDIR/fg.err"
    log "fg_status=$?"
    bg >"$LOGDIR/bg.out" 2>"$LOGDIR/bg.err"
    log "bg_status=$?"
    if "$FGPG" state "$pid" >/dev/null; then log job=alive; else log job=gone; fi
    jobs -p >"$LOGDIR/jobs_p.out" 2>&1
    if grep -qx "$pid" "$LOGDIR/jobs_p.out"; then log jobs_p=pid; else log jobs_p=other; fi
    end_job h
    ;;
  *)
    echo "script.sh: unknown mode $mode" >&2
    exit 2
    ;;
esac
exit 0
