#!/bin/sh
# flock_demo.sh MODE LOCKFILE: the shell side of flock(1) locking.
#
# Modes:
#   singleton       take the lock on fd 9 for a ( ... ) 9>>LOCK subshell, or print "busy"
#   exec-form       exec 9>>LOCK, then lock fd 9 from an external utility
#   inherit         leave a background child holding the lock after the critical section
#   inherit-closed  the same, but close fd 9 in the child with 9>&-
#   inherit-closed-exec  the same, but with exec /bin/sleep 5 9>&- & (yash keeps a copy otherwise)
#   rm-race         holder A deletes LOCK on its way out while B waits; C then locks a new file
#   keep-file       the same sequence without the rm: C is shut out
#
# The lock utility is flock(1) (util-linux or BusyBox) when present, else lockf(1)
# (FreeBSD and macOS); both lock the descriptor with flock(2).

try_lock() {
  if command -v flock >/dev/null 2>&1; then
    flock -n "$1"
  elif command -v lockf >/dev/null 2>&1; then
    lockf -s -t 0 "$1"
  else
    echo "flock_demo: no flock or lockf on PATH" >&2
    return 127
  fi
}

# lock_wait FD: block until the lock on FD is ours.
lock_wait() {
  if command -v flock >/dev/null 2>&1; then
    flock "$1"
  elif command -v lockf >/dev/null 2>&1; then
    lockf -s -t 20 "$1"
  else
    echo "flock_demo: no flock or lockf on PATH" >&2
    return 127
  fi
}

# probe_lock LOCK: open LOCK anew and report whether the lock is free.
probe_lock() {
  (
    if try_lock 9; then echo free; else echo busy; fi
  ) 9>>"$1"
}

mode=$1
lock=$2

case $mode in
  singleton)
    (
      if ! try_lock 9; then
        echo busy
        exit 75
      fi
      echo acquired
      /bin/sleep 3
      echo released
    ) 9>>"$lock"
    ;;
  exec-form)
    exec 9>>"$lock"
    if try_lock 9 2>/dev/null; then
      echo locked
    else
      echo "not locked"
    fi
    exec 9>&-
    ;;
  inherit | inherit-closed | inherit-closed-exec)
    pidfile=$lock.pid
    (
      try_lock 9 || exit 1
      case $mode in
        inherit) /bin/sleep 5 & ;;
        inherit-closed) /bin/sleep 5 9>&- & ;;
        *) exec /bin/sleep 5 9>&- & ;;
      esac
      echo "$!" >"$pidfile"
    ) 9>>"$lock"
    probe_lock "$lock"
    read -r pid <"$pidfile"
    kill "$pid"
    ;;
  rm-race | keep-file)
    marker=$lock.held
    # shellcheck disable=SC2094 # deleting the open lock file is what this mode shows
    (
      lock_wait 9 || exit 1
      echo A >"$marker"
      /bin/sleep 2
      if [ "$mode" = rm-race ]; then rm -f "$lock"; fi
    ) 9>>"$lock" &
    apid=$!
    n=0
    while [ ! -s "$marker" ] && [ "$n" -lt 100 ]; do
      /bin/sleep 0.1
      n=$((n + 1))
    done
    (
      lock_wait 9 && echo "B acquired"
      /bin/sleep 3
    ) 9>>"$lock" &
    bpid=$!
    wait "$apid"
    /bin/sleep 1
    (
      if try_lock 9; then echo "C acquired"; else echo "C blocked"; fi
    ) 9>>"$lock"
    wait "$bpid"
    ;;
  *)
    echo "usage: flock_demo.sh singleton|exec-form|inherit|inherit-closed|inherit-closed-exec|rm-race|keep-file LOCKFILE" >&2
    exit 2
    ;;
esac
