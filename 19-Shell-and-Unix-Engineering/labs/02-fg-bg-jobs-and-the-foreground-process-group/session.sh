#!/bin/sh
# session.sh: the functions a typed session uses; probe.sh copies it to LOGDIR/s and the harness
# types `. ./s` at the interactive shell under test.
# Each job is started by a command line typed at the prompt, never from inside a function, so the
# shell treats it exactly as it treats a user's command; the functions only inspect the result.
# Needs FGPG (the compiled fgpg.c) and LOGDIR in the environment.
# shellcheck disable=SC2329 # every function is called from a typed command line, not from this file

log() {
  printf '%s\n' "$*" >>"$LOGDIR/session.log"
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

# wait_line FILE PATTERN SECONDS: succeed once a line of FILE matches PATTERN, fail after SECONDS.
wait_line() {
  wl_tries=$(($3 * 10))
  while ! grep -q "$2" "$1" 2>/dev/null; do
    if [ "$wl_tries" -le 0 ]; then
      return 1
    fi
    "$FGPG" msleep 100
    wl_tries=$((wl_tries - 1))
  done
  return 0
}

# wait_state PID STATE SECONDS: succeed once the process is in STATE (R, S, T), fail after SECONDS.
wait_state() {
  ws_tries=$(($3 * 10))
  while :; do
    ws_state=$("$FGPG" state "$1")
    if [ "$ws_state" = "$2" ]; then
      return 0
    fi
    if [ "$ws_tries" -le 0 ]; then
      return 1
    fi
    "$FGPG" msleep 100
    ws_tries=$((ws_tries - 1))
  done
}

# wait_gone PID SECONDS: succeed once the process has exited (a zombie counts), fail after SECONDS.
wait_gone() {
  wg_tries=$(($2 * 10))
  while "$FGPG" state "$1" >/dev/null; do
    if [ "$wg_tries" -le 0 ]; then
      return 1
    fi
    "$FGPG" msleep 100
    wg_tries=$((wg_tries - 1))
  done
  return 0
}

# wait_jobs_stopped FILE COUNT: write `jobs` to FILE until it lists COUNT stopped jobs (up to 5 s).
# The kernel stops a process before the shell has collected the wait status, so one `jobs` call races.
# Standard error is kept out of FILE: an interactive shell that notices the stop while `jobs` runs
# writes its "Stopped" notice there, and FILE must hold one listing, not a listing plus a notice.
wait_jobs_stopped() {
  wj_tries=50
  while :; do
    jobs >"$1" 2>/dev/null
    wj_count=$(grep -ciE 'stopped|suspended' "$1")
    if [ "$wj_count" -ge "$2" ] || [ "$wj_tries" -le 0 ]; then
      return 0
    fi
    "$FGPG" msleep 100
    wj_tries=$((wj_tries - 1))
  done
}

# snap LABEL [NAME...]: log which process group owns the terminal right now.
# The observer runs in a command substitution: no job is created for it and it is never given the
# terminal, so the answer is the state the shell was in when it read this command.
snap() {
  sn_label=$1
  shift
  sn_line=$("$FGPG" who "$$" "$LOGDIR" "$@")
  log "$sn_label $sn_line"
}

# job_number ID: set jn_number to the job number `jobs ID` reports, or to `none`.
# It runs in the current shell on purpose: a command substitution is a subshell, and POSIX.1-2024
# only says that `jobs` may work there.
job_number() {
  jn_number=none
  jn_line=
  jobs "$1" >"$LOGDIR/id.out" 2>/dev/null
  read -r jn_line <"$LOGDIR/id.out"
  case $jn_line in
    \[[0-9]*\]*)
      jn_line=${jn_line#\[}
      jn_number=${jn_line%%\]*}
      ;;
    *) ;;
  esac
}

# list_jobs: `jobs -p` from inside a function, for the command substitution probe.
list_jobs() {
  jobs -p
}

# cleanup NAME...: kill every named helper, with its whole process group when that is not the
# shell's own group, wait until each is really gone, and let the shell collect its jobs, quietly.
# The group matters in mksh: a function run with `&` is a forked mksh that stops on SIGTTOU when
# its child dies. The wait matters everywhere: SIGKILL is not instant, and a shell that still
# lists a stopped job refuses the typed `exit`.
cleanup() {
  cl_shell=$("$FGPG" pgid "$$")
  for cl_name in "$@"; do
    if [ -e "$LOGDIR/$cl_name.pid" ]; then
      read -r cl_pid cl_pgrp <"$LOGDIR/$cl_name.pid"
      if [ "$cl_pgrp" != "$cl_shell" ]; then
        { kill -s KILL -- "-$cl_pgrp"; } 2>/dev/null
      fi
      { kill -s KILL "$cl_pid"; } 2>/dev/null
      wait_gone "$cl_pid" 5 || log "cleanup_left=$cl_name"
    fi
  done
  wait 2>/dev/null
  jobs >/dev/null 2>&1
}

# after_stop NAME: typed after the SUSP character suspended the foreground job NAME.
after_stop() {
  as_status=$?
  log "susp_status=$as_status"
  snap stopped "$1"
  as_echo=$("$FGPG" echoflag)
  log "while_stopped $as_echo"
  wait_jobs_stopped "$LOGDIR/jobs_stopped.out" 1
}

# after_bg: typed after `bg` resumed job a in the background; then stop it again with a job ID.
after_bg() {
  ab_status=$?
  ab_bang=$!
  log "bg_status=$ab_status"
  read -r ab_pid _ <"$LOGDIR/a.pid"
  # Job a was started in the foreground, so only bg can have made it the job that $! names.
  if [ "$ab_bang" = "$ab_pid" ]; then log bang_after_bg=job; else log bang_after_bg=other; fi
  wait_line "$LOGDIR/a.log" '^cont ' 8 || log no_cont_after_bg
  wait_line "$LOGDIR/a.log" '^role bg' 8 || log no_bg_role
  snap after_bg a
  jobs >"$LOGDIR/jobs_running.out" 2>/dev/null
  kill -s STOP %%
  wait_state "$ab_pid" T 8 || log not_stopped_again
  wait_jobs_stopped "$LOGDIR/jobs_restopped.out" 1
}

# after_fg NAME: typed after `fg`; the status fg left behind is the job's.
after_fg() {
  af_status=$?
  log "fg_status=$af_status"
  snap after_fg "$1"
  cleanup "$1"
}

# markers_check: three background jobs; stopping them moves the current (+) and previous (-) marks.
markers_check() {
  wait_file "$LOGDIR/one.ready" 8 || log one_not_ready
  wait_file "$LOGDIR/two.ready" 8 || log two_not_ready
  wait_file "$LOGDIR/three.ready" 8 || log three_not_ready
  read -r mc_one _ <"$LOGDIR/one.pid"
  read -r mc_two _ <"$LOGDIR/two.pid"
  jobs >"$LOGDIR/jobs_running.out" 2>/dev/null
  jobs -l >"$LOGDIR/jobs_l.out" 2>/dev/null
  jobs -p >"$LOGDIR/jobs_p.out" 2>/dev/null
  kill -s STOP "$mc_one"
  wait_jobs_stopped "$LOGDIR/jobs_one_stopped.out" 1
  kill -s STOP "$mc_two"
  wait_jobs_stopped "$LOGDIR/jobs_two_stopped.out" 2
  cleanup one two three
}

# The two jobs of the job ID session: each is a function, so its command text is one known word.
alpha() {
  "$FGPG" hold "$LOGDIR" alpha
}
bravo() {
  "$FGPG" hold "$LOGDIR" bravo
}

# jobids_check: every job ID form of POSIX.1-2024 XBD 3.182, resolved to a job by `jobs ID`.
jobids_check() {
  wait_file "$LOGDIR/alpha.ready" 8 || log alpha_not_ready
  wait_file "$LOGDIR/bravo.ready" 8 || log bravo_not_ready
  jobs >"$LOGDIR/jobs.out" 2>/dev/null
  # alpha was started first, so it has the lower of the two job numbers `jobs` prints.
  ji_alpha=none
  ji_bravo=none
  while read -r ji_line; do
    case $ji_line in
      \[[0-9]*\]*)
        ji_line=${ji_line#\[}
        ji_line=${ji_line%%\]*}
        if [ "$ji_alpha" = none ]; then
          ji_alpha=$ji_line
        elif [ "$ji_line" -lt "$ji_alpha" ]; then
          ji_bravo=$ji_alpha
          ji_alpha=$ji_line
        else
          ji_bravo=$ji_line
        fi
        ;;
      *) ;;
    esac
  done <"$LOGDIR/jobs.out"
  log "numbers=$ji_alpha,$ji_bravo"
  for ji_id in "%$ji_alpha" "%$ji_bravo" %+ %% %- %alp '%?rav' '%?lph' %99 %zulu; do
    job_number "$ji_id"
    case $jn_number in
      "$ji_alpha") ji_name=alpha ;;
      "$ji_bravo") ji_name=bravo ;;
      *) ji_name=$jn_number ;;
    esac
    case $ji_id in
      "%$ji_alpha") ji_id=%first ;;
      "%$ji_bravo") ji_id=%second ;;
      *) ;;
    esac
    log "$ji_id=$ji_name"
  done
  # `jobs` in a command substitution runs in a subshell environment; count the lines it printed.
  ji_listed=$(jobs -p)
  ji_count=$(printf '%s\n' "$ji_listed" | grep -c .)
  log "comsub_simple=$ji_count"
  ji_listed=$(list_jobs)
  ji_count=$(printf '%s\n' "$ji_listed" | grep -c .)
  log "comsub_function=$ji_count"
  cleanup alpha bravo
}

# pipeline_check: a two-process background job; which process is `$!`, which one leads the group,
# and whom `kill` reaches.
pipeline_check() {
  pc_bang=$!
  wait_file "$LOGDIR/first.ready" 8 || log first_not_ready
  wait_file "$LOGDIR/last.ready" 8 || log last_not_ready
  read -r pc_first pc_first_pgrp <"$LOGDIR/first.pid"
  read -r pc_last pc_last_pgrp <"$LOGDIR/last.pid"
  jobs -p >"$LOGDIR/jobs_p.out" 2>/dev/null
  case $pc_bang in
    "$pc_last") log bang=last ;;
    "$pc_first") log bang=first ;;
    *) log bang=other ;;
  esac
  case $pc_first_pgrp in
    "$pc_first") log leader=first ;;
    "$pc_last") log leader=last ;;
    *) log leader=other ;;
  esac
  if [ "$pc_first_pgrp" = "$pc_last_pgrp" ]; then log one_group=yes; else log one_group=no; fi
  if grep -qx "$pc_first_pgrp" "$LOGDIR/jobs_p.out"; then log jobs_p=pgid; else log jobs_p=other; fi
  { kill "$pc_bang"; } 2>/dev/null
  wait_gone "$pc_last" 3 || log last_survived
  "$FGPG" msleep 1000
  if "$FGPG" state "$pc_first" >/dev/null; then log after_kill_bang_first=alive; else log after_kill_bang_first=gone; fi
  { kill %%; } 2>/dev/null
  if wait_gone "$pc_first" 3; then log after_kill_job_first=gone; else log after_kill_job_first=alive; fi
  cleanup first last
}

touch "$LOGDIR/defs.ready"
