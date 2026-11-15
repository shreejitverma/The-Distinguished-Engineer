#!/usr/bin/env bash
# pool.sh MAXJOBS NAME:SECONDS:STATUS...: run each task (sleep SECONDS, then exit STATUS) with at
# most MAXJOBS running at once, and report every task the moment it ends.
# wait -n -p tells the pool which task ended; when it reports no child while tasks are still
# outstanding (bash 5.1 to 5.2.29 can lose a job that ended before wait -n was called), the pool
# recovers each status with wait PID, which reads the shell's saved status list.
# POOL_WORK_SECONDS=N makes the pool run N seconds of foreground work (an external sleep) after
# each launch, the way a real pool runs commands between waits; tasks then end outside wait -n.
# Exit status: 0 if every task exited 0, 1 if any failed, 2 for a usage error or a shell
# without a usable wait -n -p (bash 5.1 or later).
# Example: pool.sh 2 a:2:0 b:0:3  ->  done b status=3, done a status=0, summary: 2 tasks, 1 failed

if ! (
  (exit 5) &
  p=$!
  wait -n -p v 2>/dev/null
  [ "$?" -eq 5 ] && [ "${v-}" = "$p" ]
); then
  echo "pool.sh: this shell has no usable wait -n -p (bash 5.1 or later)" >&2
  exit 2
fi

set -u

usage() {
  echo "usage: pool.sh MAXJOBS NAME:SECONDS:STATUS..." >&2
  exit 2
}

[ "$#" -ge 2 ] || usage
max=$1
shift
case $max in
  '' | *[!0-9]* | 0) usage ;;
  *) ;;
esac

declare -A task_of # pid -> task name, for the tasks still outstanding
running=0
failed=0
total=$#

# finish PID STATUS: report one ended task and forget it.
finish() {
  echo "done ${task_of[$1]} status=$2"
  [ "$2" -eq 0 ] || failed=$((failed + 1))
  unset 'task_of[$1]'
  running=$((running - 1))
}

# reap_one: block until one outstanding task has ended, then report it.
reap_one() {
  local pid status
  wait -n -p pid
  status=$?
  if [ -n "${pid-}" ]; then
    finish "$pid" "$status"
    return
  fi
  if [ "$status" -gt 128 ]; then
    return # a trapped signal interrupted wait -n; nothing was reaped
  fi
  # wait -n saw no child, yet tasks are outstanding: their statuses were already collected.
  for pid in "${!task_of[@]}"; do
    wait "$pid"
    finish "$pid" "$?"
    return
  done
  echo "pool.sh: wait -n found no child and no task is outstanding" >&2
  exit 2
}

for spec in "$@"; do
  IFS=: read -r name seconds code <<<"$spec"
  while [ "$running" -ge "$max" ]; do
    reap_one
  done
  (
    /bin/sleep "$seconds"
    exit "$code"
  ) &
  task_of[$!]=$name
  running=$((running + 1))
  if [ "${POOL_WORK_SECONDS:-0}" -gt 0 ]; then
    /bin/sleep "$POOL_WORK_SECONDS"
  fi
done

while [ "$running" -gt 0 ]; do
  reap_one
done

echo "summary: $total tasks, $failed failed"
[ "$failed" -eq 0 ]
