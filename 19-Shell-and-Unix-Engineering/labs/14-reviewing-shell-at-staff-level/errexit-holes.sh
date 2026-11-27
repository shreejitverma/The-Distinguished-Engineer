#!/bin/sh
# errexit-holes.sh MODE: one review exhibit per hole in `set -e`; each prints "start", then
# "survived" or a marker if the shell reached that point.
# MODE: if-context, or-list, fixed-if, and-list-return, bang, assign, export, export-fixed,
# pipeline, pipefail.
# shellcheck disable=SC2310,SC2311,SC2312,SC2251,SC2317,SC2155,SC2046,SC3040 # the errexit holes are the exhibit; pipefail is probed in a subshell
mode=${1:?usage: errexit-holes.sh MODE}
set -e
echo start
case $mode in
  if-context)
    # errexit is ignored for the whole body of f because f runs as an `if` condition.
    f() {
      false
      echo reached-in-f
    }
    if f; then echo cond-true; fi
    ;;
  or-list)
    # the left side of || is exempt too, so f runs to completion and returns 1 from `return`.
    f() {
      false
      echo reached-in-f
      return 1
    }
    f || echo handled
    ;;
  fixed-if)
    # an explicit `|| return` makes the function stop even when it is used as a condition.
    f() {
      false || return 1
      echo reached-in-f
    }
    if f; then echo cond-true; else echo cond-false; fi
    ;;
  and-list-return)
    # the failed test is not the last command of the list, so errexit ignores it inside check,
    # but check returns 1 and that fails the simple command in the caller.
    check() { [ -n "$1" ] && echo has-arg; }
    check ""
    echo survived
    ;;
  bang)
    # a pipeline that begins with ! is exempt, even when it "fails".
    ! true
    echo survived
    ;;
  assign)
    # an assignment-only command takes the status of its command substitution (XCU 2.9.1.3).
    v=$(false)
    echo survived
    ;;
  export)
    # export is a command with a name: its own status (0) replaces the substitution's.
    export v=$(false)
    echo survived
    ;;
  export-fixed)
    # ShellCheck SC2155: declare and assign separately.
    v=$(false)
    export v
    echo survived
    ;;
  pipeline)
    # only the last command of a pipeline decides the status unless pipefail is on.
    false | cat
    echo survived
    ;;
  pipefail)
    # set is a special built-in: an unknown option would end the shell, so probe in a subshell.
    # errexit stays off here: ksh93u+ 2012 sets the pipeline status but does not exit on it.
    set +e
    if (set -o pipefail) 2>/dev/null; then
      set -o pipefail
      false | cat
      echo "status=$?"
    else
      echo no-pipefail
    fi
    ;;
  *)
    echo "errexit-holes.sh: unknown mode: $mode" >&2
    exit 64
    ;;
esac
