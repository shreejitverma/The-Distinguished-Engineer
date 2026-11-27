#!/bin/sh
# pool.sh STRATEGY N EVENTS SPEC...: run one worker.sh per SPEC with at most N running at once.
# SPEC is SECS or SECS:EXIT (or "hold", see worker.sh); worker IDs are 1, 2, 3 ... in SPEC order.
# STRATEGY: batch (start N, wait for all N), oldest (wait for the oldest PID), poll (kill -0 sweep),
# waitn (wait -n, a bash/BusyBox extension).
# Prints "jobs=T ok=K failed=F codes=C..." and exits 0 if F is 0, 1 if any worker failed,
# 2 on usage error or an unsupported strategy, 143 after SIGTERM or SIGINT.
if [ -n "${ZSH_VERSION-}" ]; then
  emulate -R sh # native zsh does not split unquoted parameters; the pool keeps PID lists in words
fi
unset IFS
here=${0%/*}

usage() {
  echo "usage: pool.sh batch|oldest|poll|waitn N EVENTS SPEC..." >&2
  exit 2
}
[ "$#" -ge 4 ] || usage
strategy=$1
limit=$2
events=$3
shift 3
case $limit in
  '' | *[!0-9]* | 0) usage ;;
  *) ;;
esac

live='' # PIDs started and not yet waited for, oldest first, each preceded by a space
total=0 # workers started
ok=0
failed=0
codes=''

# start ID SPEC: run one worker in the background; sets pid and appends it to live.
start() {
  secs=${2%%:*}
  case $2 in
    *:*) code=${2#*:} ;;
    *) code=0 ;;
  esac
  "$here/worker.sh" "$events" "$1" "$secs" "$code" &
  pid=$!
  live="$live $pid"
  total=$((total + 1))
}

# reap STATUS: record one worker's wait status.
reap() {
  if [ "$1" -eq 0 ]; then
    ok=$((ok + 1))
  else
    failed=$((failed + 1))
    codes="$codes $1"
  fi
}

# forget PID: drop PID from live.
forget() {
  newlive=''
  for p in $live; do
    [ "$p" = "$1" ] || newlive="$newlive $p"
  done
  live=$newlive
}

# count_live: set nlive to the number of PIDs in live.
count_live() {
  nlive=0
  for p in $live; do
    nlive=$((nlive + 1))
  done
}

# shellcheck disable=SC2329 # on_term is only invoked through the trap below
# on_term: kill every worker we still know about, reap them, and exit 143.
on_term() {
  trap - TERM INT
  case $strategy in
    waitn)
      # shellcheck disable=SC3045 # jobs -r is used only by the waitn strategy, which needs bash or BusyBox
      running=$(jobs -rp)
      # shellcheck disable=SC2086,SC2248 # running is a word list on purpose
      [ -z "$running" ] || kill $running 2>/dev/null
      ;;
    *)
      # shellcheck disable=SC2086,SC2248 # live is a word list on purpose
      [ -z "$live" ] || kill $live 2>/dev/null
      ;;
  esac
  wait
  exit 143
}

report() {
  echo "jobs=$total ok=$ok failed=$failed codes=${codes# }"
  [ "$failed" -eq 0 ] || exit 1
  exit 0
}

trap on_term TERM INT
id=0

case $strategy in
  batch)
    # Barrier: start up to N workers, wait for all of them, repeat. A slow worker idles the rest.
    for spec in "$@"; do
      id=$((id + 1))
      start "$id" "$spec"
      count_live
      if [ "$nlive" -ge "$limit" ]; then
        for p in $live; do
          wait "$p"
          reap "$?"
        done
        live=''
      fi
    done
    for p in $live; do
      wait "$p"
      reap "$?"
    done
    live=''
    ;;
  oldest)
    # FIFO: when N are running, wait for the oldest PID. A slow oldest worker idles the other slots.
    for spec in "$@"; do
      id=$((id + 1))
      count_live
      if [ "$nlive" -ge "$limit" ]; then
        # shellcheck disable=SC2086,SC2248 # live is a word list on purpose
        set -- $live
        head=$1
        wait "$head"
        reap "$?"
        forget "$head"
      fi
      start "$id" "$spec"
    done
    for p in $live; do
      wait "$p"
      reap "$?"
    done
    live=''
    ;;
  poll)
    # Sweep: kill -0 every live PID, harvest the ones that are gone, sleep, repeat.
    # A shell that never reaps a finished child would spin here forever; wrap the pool in timeout(1).
    pending=$#
    while [ "$pending" -gt 0 ] || [ -n "$live" ]; do
      progressed=0
      for p in $live; do
        if ! kill -0 "$p" 2>/dev/null; then
          wait "$p"
          reap "$?"
          forget "$p"
          progressed=1
        fi
      done
      count_live
      while [ "$pending" -gt 0 ] && [ "$nlive" -lt "$limit" ]; do
        id=$((id + 1))
        spec=$1
        shift
        pending=$((pending - 1))
        start "$id" "$spec"
        nlive=$((nlive + 1))
        progressed=1
      done
      [ "$progressed" -eq 1 ] || /bin/sleep 0.1
    done
    ;;
  waitn)
    # wait -n returns when any one child ends; the shell must support it. Probe in a subshell,
    # because an unknown option to a builtin can end the shell that runs it.
    if ! (
      /bin/sleep 0 &
      # shellcheck disable=SC3045 # the probe exists to find out whether wait -n works
      wait -n
    ) >/dev/null 2>&1; then
      echo "pool.sh: this shell has no usable wait -n" >&2
      exit 2
    fi
    running=0
    for spec in "$@"; do
      id=$((id + 1))
      if [ "$running" -ge "$limit" ]; then
        # shellcheck disable=SC3045 # reached only when the probe above passed
        wait -n
        reap "$?"
        running=$((running - 1))
      fi
      start "$id" "$spec"
      running=$((running + 1))
    done
    while [ "$running" -gt 0 ]; do
      # shellcheck disable=SC3045 # reached only when the probe above passed
      wait -n
      reap "$?"
      running=$((running - 1))
    done
    live=''
    ;;
  *) usage ;;
esac
report
