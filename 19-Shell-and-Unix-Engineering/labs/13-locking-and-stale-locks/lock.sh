#!/bin/sh
# lock.sh - a POSIX sh mutual-exclusion lock with an owner record and safe stale-lock breaking.
# Source it: . ./lock.sh
#
# The lock is a symbolic link LOCK -> PID:START:HOST made by `ln -s`, which fails when LOCK
# exists (POSIX ln without -f), so existence and owner record appear in one atomic step and a
# reader never sees a lock without an owner. START is the holder's process start time, so a
# reused PID does not make a dead holder look alive. Pass LOCK as an absolute or ./ path.
#
# lock_try LOCK          0 acquired, 1 busy (or stale but lost the break race), 2 error
# lock_acquire LOCK SECS retry lock_try once a second for up to SECS seconds
# lock_release LOCK      remove LOCK only if this process owns it (1 if it does not)
# lk_alive RECORD        0 owner alive, 1 owner dead or PID reused, 2 cannot tell (other host, bad record)
#
# Variables use the lk_ prefix because POSIX sh has no local variables.

lk_host() {
  uname -n
}

# lk_start PID: print a token for the start time of PID; fail if PID does not exist.
lk_start() {
  if [ -r "/proc/$1/stat" ]; then
    # Field 22 is starttime in clock ticks since boot; comm (field 2) may contain spaces and
    # parentheses, so strip through the last ") " and take field 20 of the rest.
    awk '{ sub(/.*\) /, ""); print $20 }' "/proc/$1/stat" 2>/dev/null
  elif [ -d /proc/1 ]; then
    return 1 # Linux procfs is mounted and has no entry: the process is gone
  else
    lk_t=$(ps -o lstart= -p "$1" 2>/dev/null) || return 1
    [ -n "$lk_t" ] || return 1
    printf '%s\n' "$lk_t" | tr -cd '0-9A-Za-z'
  fi
}

# lk_record PID: print PID:START:HOST for a running PID.
lk_record() {
  lk_s=$(lk_start "$1") || return 1
  [ -n "$lk_s" ] || return 1
  lk_h=$(lk_host) || return 1
  printf '%s:%s:%s\n' "$1" "$lk_s" "$lk_h"
}

# lk_read LOCK: print the owner record stored in LOCK; fail if LOCK is not a symbolic link.
lk_read() {
  readlink "$1" 2>/dev/null
}

lk_alive() {
  lk_pid=${1%%:*}
  lk_rest=${1#*:}
  lk_tok=${lk_rest%%:*}
  lk_rhost=${lk_rest#*:}
  case $lk_pid in
    '' | *[!0-9]*) return 2 ;;
    *) ;;
  esac
  [ "$lk_rest" != "$1" ] || return 2
  [ "$lk_rhost" != "$lk_rest" ] || return 2
  lk_me=$(lk_host) || return 2
  [ "$lk_rhost" = "$lk_me" ] || return 2 # another host's PID means nothing here
  lk_now=$(lk_start "$lk_pid") || return 1
  [ "$lk_now" = "$lk_tok" ] || return 1 # the PID exists but belongs to a newer process
  return 0
}

# lk_break LOCK SEEN: remove LOCK only if it still holds the stale record SEEN.
# The re-check runs under a second lock (LOCK.break, a directory) so that two breakers cannot
# both decide "stale" and then remove each other's fresh lock. LK_BREAK_DELAY (seconds) widens
# the window between the first check and the break, so the lab can show why the re-check matters.
lk_break() {
  if [ -n "${LK_BREAK_DELAY-}" ]; then
    sleep "$LK_BREAK_DELAY"
  fi
  mkdir "$1.break" 2>/dev/null || return 1
  lk_again=$(lk_read "$1")
  if [ "$lk_again" = "$2" ]; then
    rm -f "$1"
  fi
  rmdir "$1.break"
}

lock_try() {
  lk_self=$(lk_record "$$") || return 2
  if ln -s "$lk_self" "$1" 2>/dev/null; then
    lk_got=$(lk_read "$1")
    [ "$lk_got" = "$lk_self" ] && return 0
    # LOCK was (or resolved to) a directory, so ln made LOCK/RECORD inside it: undo, refuse.
    rm -f "$1/$lk_self"
    return 2
  fi
  lk_seen=$(lk_read "$1") || return 1 # vanished or not a symlink: report busy, caller retries
  lk_alive "$lk_seen"
  [ "$?" -eq 1 ] || return 1 # alive, or cannot tell: never break
  lk_break "$1" "$lk_seen" || return 1
  ln -s "$lk_self" "$1" 2>/dev/null
}

lock_acquire() {
  lk_left=$2
  while :; do
    lock_try "$1"
    lk_rc=$?
    [ "$lk_rc" -ne 1 ] && return "$lk_rc"
    [ "$lk_left" -gt 0 ] || return 1
    lk_left=$((lk_left - 1))
    sleep 1
  done
}

lock_release() {
  lk_self=$(lk_record "$$") || return 2
  lk_got=$(lk_read "$1") || return 1
  [ "$lk_got" = "$lk_self" ] || return 1
  rm -f "$1"
}
