#!/bin/sh
# errors-probe: mechanism probes for the shell error handling pattern catalog.
# Each mode computes what it reports from statuses the shell under test produced.
# Usage: errors-probe.sh MODE [SCRATCH_DIR]
#   argzero        what $0 names inside a function (the die() prefix)
#   die_subshell   die inside $(...) ends only the substitution's subshell
#   capture        where $? still holds a failure, and where it is already lost
#   declare_mask   export x=$(cmd) and local x=$(cmd) report the declaration, not cmd
#   pipe_status    portable upstream status through descriptor 3
#   pipe_loop      a failure flag set inside a piped while loop
#   wait_status    bare wait versus wait PID for failing background jobs
#   param_guard    ${var:?} aborts the (sub)shell before an empty path is used
#   write_error    printf to a closed standard output
#   tristate       grep 0, 1, and >1 told apart with case
#   accumulate     keep going over every item, then exit with the failure count
#   find_exec      find -exec ... ; hides failures, -exec ... + reports them (needs SCRATCH_DIR)
#   xargs_status   xargs status when one invocation fails

mode=${1:?usage: errors-probe.sh MODE [SCRATCH_DIR]}

# Capture the program name at top level: native zsh sets $0 to the function
# name inside a function (FUNCTION_ARGZERO), so "${0##*/}" in die would say "die".
prog=${0##*/}

# die: the one-line diagnostic and exit used by every pattern below.
die() {
  printf '%s: %s\n' "$prog" "$*" >&2
  exit 1
}

case $mode in
  argzero)
    # What $0 names inside a function, next to the name captured at top level.
    inner() { printf '%s' "${0##*/}"; }
    fn=$(inner)
    printf 'top=%s in_function=%s\n' "$prog" "$fn"
    ;;

  die_subshell)
    # exit inside $(...) leaves only the subshell (POSIX.1-2024 XCU 2.15 exit);
    # the assignment then carries the substitution's status (XCU 2.9.1.3).
    v=$(die 'boom' 2>/dev/null)
    st=$?
    printf 'after_die=reached st=%d value=[%s]\n' "$st" "$v"
    ;;

  capture)
    # 1. In the else branch, $? is still the condition's status (XCU 2.9.4.4 note).
    if sh -c 'exit 7'; then
      else_rc=none
    else
      else_rc=$?
    fi
    # 2. After "if ! cmd", $? is the inverted status: 0, not 7.
    if ! sh -c 'exit 7'; then
      not_rc=$?
    else
      not_rc=none
    fi
    # 3. cmd || rc=$? keeps the status without stopping a set -e script.
    or_rc=0
    sh -c 'exit 7' || or_rc=$?
    # 4. Any command in between overwrites $?.
    sh -c 'exit 7'
    echo 'logging the failure' >/dev/null
    # shellcheck disable=SC2320 # reading the overwritten status is the point
    late_rc=$?
    printf 'else=%s not=%s or=%s late=%s\n' "$else_rc" "$not_rc" "$or_rc" "$late_rc"
    ;;

  declare_mask)
    # A bare assignment has the substitution's status; a declaration utility reports its own.
    x=$(sh -c 'exit 3')
    bare=$?
    # shellcheck disable=SC2155 # the masked status is what this probe measures
    export y="$(sh -c 'exit 3')"
    exported=$?
    # local is not POSIX; detect it before measuring it.
    # shellcheck disable=SC3043 # local is feature-detected here
    if (
      f() { local z=1; }
      f
    ) 2>/dev/null; then
      # shellcheck disable=SC2034,SC2155,SC3043 # z is never read; the masked status is what this probe measures
      g() {
        local z="$(sh -c 'exit 3')"
        printf '%d' "$?"
      }
      loc=$(g)
    else
      loc=unsupported
    fi
    printf 'bare=%d export=%d local=%s x=[%s] y=[%s]\n' "$bare" "$exported" "$loc" "$x" "$y"
    ;;

  pipe_status)
    # Without pipefail the pipeline reports only its last stage (XCU 2.9.2).
    # The upstream stage writes its own status to descriptor 3, which the
    # command substitution captures; the data goes to the saved stdout (fd 4).
    exec 4>&1
    up=$(
      {
        {
          printf 'data\n'
          sh -c 'exit 3'
          printf '%s\n' "$?" >&3
        } | cat >&4
      } 3>&1
    )
    printf 'upstream=%s\n' "$up"
    exec 4>&-
    ;;

  pipe_loop)
    # A flag set in the last stage of a pipeline survives only where the shell
    # runs that stage in the current environment (XCU 2.13 allows either).
    failed=0
    printf 'a\nb\n' | while read -r item; do
      # shellcheck disable=SC2030 # the subshell modification is what this probe measures
      case $item in
        b) failed=1 ;;
        *) : ;;
      esac
    done
    # shellcheck disable=SC2031 # reads the flag the loop may have set in a subshell
    printf 'failed=%d\n' "$failed"
    ;;

  wait_status)
    # wait with no operands returns 0 once every job is done, whatever they returned (XCU wait).
    sh -c 'exit 4' &
    wait
    bare=$?
    # wait PID returns that job's status, so wait for each PID you started.
    sh -c 'exit 4' &
    p1=$!
    sh -c 'exit 0' &
    p2=$!
    wait "$p1"
    s1=$?
    wait "$p2"
    s2=$?
    printf 'bare=%d p1=%d p2=%d\n' "$bare" "$s1" "$s2"
    ;;

  param_guard)
    # The Steam bug shape: a failed cd leaves the variable empty, and "$root/" becomes "/".
    root=$(cd /nonexistent-dir-for-probe 2>/dev/null && pwd)
    printf 'unguarded=[%s/*]\n' "$root"
    # ${root:?} aborts a non-interactive (sub)shell with a non-zero status (XCU 2.6.2).
    (
      : "${root:?root is empty}"
      echo unreached
    )
    printf 'guard_st=%d\n' "$?"
    ;;

  write_error)
    # Writing to a closed descriptor fails with EBADF; the builtin must report it.
    # The subshell keeps stdout closed until it exits, so no buffered byte escapes later.
    (printf 'x\n') >&- 2>/dev/null
    printf 'closed_st=%d\n' "$?"
    ;;

  tristate)
    # grep: 0 selected, 1 none selected, >1 error (XCU grep EXIT STATUS).
    # "if grep" would merge "no match" with "could not read the file".
    classify() {
      rc=$1
      case $rc in
        0) printf 'match' ;;
        1) printf 'nomatch' ;;
        *) printf 'error(%d)' "$rc" ;;
      esac
    }
    printf 'root\n' | grep -q root
    a=$(classify "$?")
    printf 'root\n' | grep -q xyzzy
    b=$(classify "$?")
    grep -q root /nonexistent-file-for-probe 2>/dev/null
    c=$(classify "$?")
    printf '%s %s %s\n' "$a" "$b" "$c"
    ;;

  accumulate)
    # Keep going over every item, count failures, and fail at the end.
    failures=0
    for code in 0 3 0 5; do
      sh -c "exit $code" || failures=$((failures + 1))
    done
    printf 'failures=%d\n' "$failures"
    [ "$failures" -eq 0 ]
    ;;

  find_exec)
    dir=${2:?find_exec needs a scratch directory}
    : >"$dir/a"
    : >"$dir/b"
    find "$dir" -type f -exec false {} \;
    semi=$?
    find "$dir" -type f -exec false {} +
    plus=$?
    printf 'semicolon=%d plus=%d\n' "$semi" "$plus"
    ;;

  xargs_status)
    printf 'a\nb\n' | xargs false
    printf 'xargs=%d\n' "$?"
    ;;

  *)
    die "unknown mode: $mode"
    ;;
esac
