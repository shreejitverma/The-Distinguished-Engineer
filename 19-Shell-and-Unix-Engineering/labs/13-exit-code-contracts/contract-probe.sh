#!/bin/sh
# contract-probe.sh MODE [ARG...]: the shell and utility rules that decide
# which exit status a caller finally sees.
mode=${1-}
case $mode in
  if-not)
    # `!` maps every failure to 0, so $? inside "then" is 0, not the failure.
    if ! (exit 3); then
      echo "$?"
    fi
    ;;
  if-else)
    # The else branch still sees the condition's own status (XCU 2.9.4.4).
    if (exit 3); then
      :
    else
      echo "$?"
    fi
    ;;
  if-none)
    # An if that runs no branch completes with status 0.
    if (exit 3); then
      :
    fi
    echo "$?"
    ;;
  capture)
    # Under set -e, `|| rc=$?` records the exact status and keeps going.
    set -e
    rc=0
    (exit 3) || rc=$?
    echo "rc=$rc continued"
    ;;
  trap-keep)
    # The last command of an EXIT action does not replace the exit status.
    trap 'false' EXIT
    exit 7
    ;;
  trap-noarg)
    # POSIX: a bare exit that ends a trap action uses $? from before the action.
    trap 'false; exit' EXIT
    exit 7
    ;;
  count-raw)
    # Exit with a failure count as is: the kernel keeps only the low 8 bits.
    exit "$2"
    ;;
  count-clamped)
    # Exit with a failure count clamped to 125, the top of the safe range.
    n=$2
    [ "$n" -le 125 ] || n=125
    exit "$n"
    ;;
  test-empty)
    # An integer test on an empty value: error (2) or plain false (1)?
    count=''
    [ "$count" -eq 0 ] 2>/dev/null
    echo "$?"
    ;;
  grep-q)
    # ARG2 is unreadable, ARG3 contains a match.
    grep -q -e k -- "$2" "$3" 2>/dev/null
    echo "q=$?"
    grep -e k -- "$2" "$3" >/dev/null 2>&1
    echo "plain=$?"
    ;;
  xargs-nonzero)
    printf 'a\n' | xargs sh -c 'exit 3'
    echo "$?"
    ;;
  xargs-255)
    # shellcheck disable=SC2016 # $0 belongs to the sh -c child
    printf 'a\nb\n' | xargs -n 1 sh -c 'echo "run $0"; exit 255' 2>/dev/null
    echo "status=$?"
    ;;
  xargs-signal)
    # shellcheck disable=SC2016 # $$ belongs to the sh -c child
    printf 'a\n' | xargs sh -c 'kill -TERM $$' 2>/dev/null
    echo "$?"
    ;;
  *)
    printf 'usage: contract-probe.sh MODE [ARG...]\n' >&2
    exit 2
    ;;
esac
