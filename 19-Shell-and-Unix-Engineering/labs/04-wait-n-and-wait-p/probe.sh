#!/bin/sh
# shellcheck disable=SC3045,SC3040 # probing these non-POSIX wait and set options is the point
# probe.sh MODE: print what this shell's wait -n and wait -p do in one scenario.
# A shell without a working wait -n prints "unsupported: wait -n"; a shell that has -n
# but no -p prints "unsupported: wait -p" for the modes that need -p.
# Modes:
#   support       two live jobs, the faster exits 7: does wait -n return 7 while the slow one runs?
#   ambiguity     a job exits 127, then no jobs are left: what do the status and -p report each time?
#   order         two jobs have both ended before wait -n runs: which status comes first?
#   pidarg        wait -n PID while a different job ends first
#   finished      a job ends during a foreground command, then wait -n
#   signaled      a job is killed by SIGTERM during a foreground command, then wait -n and wait PID
#   trap          a trapped SIGUSR1 arrives while wait -n -p blocks
#   procsub       the only child is a live process substitution
#   subscript     the -p name is an array element whose subscript holds a command substitution
#   rewait        wait -n, then wait PID for the same job
#   rewait-posix  the same in bash POSIX mode
# Usage: probe.sh MODE
exec 2>/dev/null

mode=${1-}

has_wait_n() {
  (
    (exit 5) &
    wait -n
    [ "$?" -eq 5 ]
  )
}

has_wait_p() {
  (
    (exit 5) &
    p=$!
    wait -n -p v
    [ "$?" -eq 5 ] && [ "${v-}" = "$p" ]
  )
}

need_n() {
  if ! has_wait_n; then
    echo "unsupported: wait -n"
    exit 0
  fi
}

need_p() {
  need_n
  if ! has_wait_p; then
    echo "unsupported: wait -p"
    exit 0
  fi
}

# label PID: print job if the -p variable named the job, unset if it stayed unset, other if not.
label() {
  if [ -z "${who+set}" ]; then
    echo unset
  elif [ "$who" = "$1" ]; then
    echo "$2"
  else
    echo other
  fi
}

case $mode in
  support)
    /bin/sleep 2 &
    slow=$!
    (
      /bin/sleep 1
      exit 7
    ) &
    wait -n
    st=$?
    if kill -0 "$slow"; then alive=yes; else alive=no; fi
    echo "status=$st slow_alive=$alive"
    kill "$slow"
    wait
    ;;
  ambiguity)
    need_p
    nosuchcommand_for_wait_lab &
    p=$!
    wait -n -p who
    first=$?
    first_who=$(label "$p" job)
    wait -n -p who
    second=$?
    second_who=$(label "$p" job)
    echo "first=$first who=$first_who second=$second who=$second_who"
    ;;
  order)
    need_n
    (
      /bin/sleep 1
      exit 11
    ) &
    (exit 22) &
    /bin/sleep 2
    wait -n
    first=$?
    wait -n
    echo "first=$first second=$?"
    ;;
  pidarg)
    need_n
    (
      /bin/sleep 1
      exit 4
    ) &
    p=$!
    (exit 9) &
    wait -n "$p"
    echo "status=$?"
    wait
    ;;
  finished)
    need_n
    (exit 3) &
    /bin/sleep 1
    wait -n
    echo "status=$?"
    ;;
  signaled)
    need_n
    /bin/sleep 5 &
    p=$!
    /bin/sleep 1
    kill -TERM "$p"
    /bin/sleep 1
    wait -n
    first=$?
    wait "$p"
    echo "wait-n=$first wait-pid=$?"
    ;;
  trap)
    need_p
    trap 'echo trapped' USR1
    /bin/sleep 3 &
    s=$!
    (
      /bin/sleep 1
      kill -USR1 $$
      /bin/sleep 1
    ) &
    wait -n -p who
    st=$?
    trap '' USR1
    if [ "$st" -gt 128 ]; then st=$(kill -l "$st"); fi
    who_label=$(label "$s" sleeper)
    echo "status=$st who=$who_label"
    kill "$s"
    wait
    ;;
  procsub)
    need_p
    # Process substitution is bash syntax; eval keeps other shells from parsing it.
    eval 'exec 3< <(/bin/sleep 1; exit 7)'
    p=$!
    wait -n -p who
    st=$?
    who_label=$(label "$p" procsub)
    echo "status=$st who=$who_label"
    exec 3<&-
    wait "$p" # reap it if wait -n did not; its status is not part of the answer
    exit 0
    ;;
  subscript)
    need_p
    exec 3>&1
    (exit 0) &
    # shellcheck disable=SC2016,SC2034 # the substitution is for wait's subscript evaluation; a is never read
    wait -n -p 'a[$(echo code-ran >&3)0]'
    echo "status=$?"
    ;;
  rewait | rewait-posix)
    need_n
    if [ "$mode" = rewait-posix ]; then
      if [ -z "${BASH_VERSION-}" ]; then
        echo "unsupported: set -o posix"
        exit 0
      fi
      set -o posix
    fi
    (exit 6) &
    p=$!
    wait -n
    first=$?
    wait "$p"
    echo "wait-n=$first wait-pid=$?"
    ;;
  *)
    echo "usage: probe.sh support|ambiguity|order|pidarg|finished|signaled|trap|procsub|subscript|rewait|rewait-posix" >&2
    exit 2
    ;;
esac
