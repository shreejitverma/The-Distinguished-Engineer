#!/bin/sh
# lkprobe.sh - one probe mode per locking failure window; lkprobe.bats runs each one.
# Usage: lkprobe.sh MODE [ARG...]
#   counter nolock|lock       two workers read-modify-write one counter; prints the final value
#   noclobber-devnull         set -C against a symlink to /dev/null, a regular file, and ln
#   noclobber-colon           does a failed `set -C; : > existing` end the shell? (special built-in)
#   stale-kill                SIGKILL a holder of the lock.sh lock; can a contender break it?
#   live                      a live holder: try, timed acquire, foreign release, then after release
#   pid-reuse                 kill -0 versus the PID:START:HOST check for a live but different PID
#   break-race naive|safe     two breakers of one stale lock; prints how many ended up holding it
#   kernel-kill MODE          flock or fcntl lock, holder killed with SIGKILL
#   kernel-inherit MODE       holder forks a child and exits; does the child still hold the lock?
#   kernel-close-trap MODE    closing an unrelated descriptor for the lock file (runs lkc)
#   kernel-unlink             remove a held flock lock file; can a second process lock the path?
# Internal modes, started by the probe itself: holder LOCK SECONDS, worker DIR nolock|lock.
# Environment: TMPDIR (required, scratch directories go there), LK_SH (the shell to start
# helper processes with, a command plus arguments; default sh), LKC (compiled lkc, kernel modes).
# Helpers start as background simple commands, never through a function: a background function
# runs in a forked subshell, so $! would name that subshell and kill -9 would miss the holder.

: "${TMPDIR:?TMPDIR must name a scratch directory}"
LK_SH=${LK_SH:-sh}
lk_dir=$(dirname "$0")
# shellcheck source=SCRIPTDIR/lock.sh
. "$lk_dir/lock.sh"

scratch() {
  d=$(mktemp -d "$TMPDIR/lk.XXXXXX") || exit 2
}

# again N CMD...: run CMD up to N times, one second apart, until it succeeds.
again() {
  ag_n=$1
  shift
  until "$@"; do
    ag_n=$((ag_n - 1))
    [ "$ag_n" -gt 0 ] || return 1
    sleep 1
  done
}

is_link() {
  [ -L "$1" ]
}

kernel_busy() {
  "$LKC" try "$1" "$2"
  [ "$?" -eq 1 ]
}

mode=${1-}
[ "$#" -gt 0 ] && shift
case $mode in
  holder)
    if [ "${LK_NAIVE-}" = 1 ]; then
      # The naive breaker: decide "stale", then remove whatever is there now.
      lk_break() {
        if [ -n "${LK_BREAK_DELAY-}" ]; then
          sleep "$LK_BREAK_DELAY"
        fi
        rm -f "$1"
      }
    fi
    if lock_try "$1"; then
      echo holder=acquired
      i=0
      while [ "$i" -lt "$2" ]; do
        sleep 1
        i=$((i + 1))
      done
      lock_release "$1"
    else
      echo holder=busy
    fi
    ;;
  worker)
    if [ "$2" = lock ]; then
      lock_acquire "$1/lock" 10 || exit 1
    fi
    n=$(cat "$1/count")
    /bin/sleep 1 # the window between read and write
    echo "$((n + 1))" >"$1/count"
    if [ "$2" = lock ]; then
      lock_release "$1/lock"
    fi
    ;;
  counter)
    scratch
    echo 0 >"$d/count"
    # shellcheck disable=SC2086,SC2248 # LK_SH is a command plus arguments, split on purpose
    $LK_SH "$0" worker "$d" "$1" >/dev/null 2>&1 &
    w1=$!
    # shellcheck disable=SC2086,SC2248 # LK_SH is a command plus arguments, split on purpose
    $LK_SH "$0" worker "$d" "$1" >/dev/null 2>&1 &
    w2=$!
    wait "$w1"
    wait "$w2"
    count=$(cat "$d/count") || exit 2
    echo "count=$count"
    ;;
  noclobber-devnull)
    scratch
    ln -s /dev/null "$d/lock"
    (
      set -C
      : >"$d/lock"
    ) 2>/dev/null
    a=$?
    (
      set -C
      : >"$d/lock"
    ) 2>/dev/null
    b=$?
    : >"$d/reg"
    (
      set -C
      : >"$d/reg"
    ) 2>/dev/null
    c=$?
    ln -s other "$d/lock" 2>/dev/null
    e=$?
    okfail() {
      if [ "$1" -eq 0 ]; then w=ok; else w=fail; fi
    }
    okfail "$a"
    wa=$w
    okfail "$b"
    wb=$w
    okfail "$c"
    wc=$w
    okfail "$e"
    we=$w
    echo "devnull_first=$wa devnull_second=$wb regular_second=$wc ln_on_devnull=$we"
    ;;
  noclobber-colon)
    scratch
    exec 2>/dev/null
    : >"$d/lock"
    set -C
    : >"$d/lock"
    echo "survived status=$?"
    ;;
  stale-kill)
    scratch
    # shellcheck disable=SC2086,SC2248 # LK_SH is a command plus arguments, split on purpose
    $LK_SH "$0" holder "$d/lock" 30 >/dev/null 2>&1 &
    hp=$!
    again 10 is_link "$d/lock" || exit 2
    {
      kill -9 "$hp"
      wait "$hp"
    } 2>/dev/null
    if is_link "$d/lock"; then after=present; else after=absent; fi
    if lock_try "$d/lock"; then c=acquired; else c=busy; fi
    echo "after_kill=$after contender=$c"
    ;;
  live)
    scratch
    # shellcheck disable=SC2086,SC2248 # LK_SH is a command plus arguments, split on purpose
    $LK_SH "$0" holder "$d/lock" 4 >/dev/null 2>&1 &
    hp=$!
    again 10 is_link "$d/lock" || exit 2
    lock_try "$d/lock"
    t=$?
    lock_acquire "$d/lock" 1
    a=$?
    lock_release "$d/lock"
    r=$?
    if is_link "$d/lock"; then still=present; else still=absent; fi
    wait "$hp"
    if lock_try "$d/lock"; then after=acquired; else after=busy; fi
    echo "try=$t acquire_1s=$a release_foreign=$r lock=$still after_holder=$after"
    ;;
  pid-reuse)
    /bin/sleep 5 &
    sp=$!
    tok=$(lk_start "$sp")
    host=$(lk_host)
    kill -0 "$sp"
    k=$?
    lk_alive "$sp:0:$host"
    wrong=$?
    lk_alive "$sp:$tok:$host"
    right=$?
    lk_alive "$sp:$tok:other.invalid"
    foreign=$?
    {
      kill "$sp"
      wait "$sp"
    } 2>/dev/null
    echo "kill0=$k wrong_token=$wrong right_token=$right foreign_host=$foreign"
    ;;
  break-race)
    scratch
    /bin/sh -c 'exit 0' &
    dp=$!
    wait "$dp"
    host=$(lk_host)
    ln -s "$dp:0:$host" "$d/lock" # a stale record: that process has exited
    naive=0
    [ "$1" = naive ] && naive=1
    # shellcheck disable=SC2086,SC2248 # LK_SH is a command plus arguments, split on purpose
    LK_NAIVE=$naive LK_BREAK_DELAY=1 $LK_SH "$0" holder "$d/lock" 4 >"$d/b1" 2>&1 &
    b1=$!
    # shellcheck disable=SC2086,SC2248 # LK_SH is a command plus arguments, split on purpose
    LK_NAIVE=$naive LK_BREAK_DELAY=3 $LK_SH "$0" holder "$d/lock" 4 >"$d/b2" 2>&1 &
    b2=$!
    wait "$b1"
    wait "$b2"
    n=$(cat "$d/b1" "$d/b2" | grep -c '^holder=acquired$')
    echo "$1: holders=$n"
    ;;
  kernel-kill)
    scratch
    "$LKC" hold "$1" "$d/lock" /bin/sleep 30 >/dev/null 2>&1 &
    hp=$!
    if again 10 kernel_busy "$1" "$d/lock"; then held=busy; else held=free; fi
    {
      kill -9 "$hp"
      wait "$hp"
    } 2>/dev/null
    if kernel_busy "$1" "$d/lock"; then after=busy; else after=free; fi
    echo "while_held=$held after_kill=$after"
    ;;
  kernel-inherit)
    scratch
    cp=$("$LKC" spawn "$1" "$d/lock" 5) || exit 2
    if kernel_busy "$1" "$d/lock"; then after=busy; else after=free; fi
    kill "$cp" 2>/dev/null
    echo "after_parent_exit=$after"
    ;;
  kernel-close-trap)
    scratch
    "$LKC" close-trap "$1" "$d/lock"
    ;;
  kernel-unlink)
    scratch
    "$LKC" hold flock "$d/lock" /bin/sleep 30 >/dev/null 2>&1 &
    hp=$!
    again 10 kernel_busy flock "$d/lock" || exit 2
    rm -f "$d/lock"
    if "$LKC" try flock "$d/lock"; then second=acquired; else second=busy; fi
    {
      kill -9 "$hp"
      wait "$hp"
    } 2>/dev/null
    echo "after_unlink=$second"
    ;;
  *)
    echo "usage: lkprobe.sh MODE [ARG...]" >&2
    exit 2
    ;;
esac
