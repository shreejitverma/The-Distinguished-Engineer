#!/bin/sh
# with-lock.sh: run a command while holding a mkdir lock, in portable POSIX sh.
# Usage: with-lock.sh LOCKDIR COMMAND [ARG...]
# mkdir is atomic (it fails with EEXIST if the name exists), so exactly one caller wins.
# Exit status: COMMAND's status; 75 if a live process holds the lock; 76 if the lock is
# stale (its recorded owner is gone). A stale lock is reported, never broken automatically:
# two waiters that both "break" it can each remove the other's fresh lock.
set -u
if [ "$#" -lt 2 ]; then
  printf 'usage: with-lock.sh LOCKDIR COMMAND [ARG...]\n' >&2
  exit 64
fi
lock=$1
shift

if ! mkdir "$lock" 2>/dev/null; then
  owner=$(cat "$lock/pid" 2>/dev/null) || owner=
  # An empty pid file means the owner is between mkdir and writing it: treat as held.
  # kill -0 also fails with EPERM for another user's live process, so share locks per user.
  if [ -z "$owner" ] || kill -0 "$owner" 2>/dev/null; then
    printf 'with-lock: %s held by pid %s\n' "$lock" "${owner:-unknown}" >&2
    exit 75
  fi
  printf 'with-lock: %s is stale (pid %s is gone); remove it by hand\n' "$lock" "$owner" >&2
  exit 76
fi

# From here on the lock is ours: release it on every exit the shell can see.
# SIGKILL cannot be trapped, which is why a mkdir lock can go stale and a kernel lock cannot.
trap 'rm -f "$lock/pid"; rmdir "$lock"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
printf '%s\n' "$$" >"$lock/pid"

"$@"
status=$?
exit "$status"
