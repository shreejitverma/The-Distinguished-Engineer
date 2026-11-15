#!/bin/sh
# reraise-probe: one mechanism per mode, for the cleanup-traps lab.
# Usage: reraise-probe.sh naive READY        trap 'cleanup; exit 143' TERM
#        reraise-probe.sh no-disarm READY    re-raise with the EXIT trap still set
#        reraise-probe.sh after-kill         does the shell run code after a self-kill?
#        reraise-probe.sh after-return       does a self-kill land once the trap returns?
#        reraise-probe.sh child-status       exit "$?" after a child died of SIGTERM
#        reraise-probe.sh double READY       a second TERM arrives during cleanup
#        reraise-probe.sh int-child MODE READY   SIGINT child for wce-loop.sh: MODE is exit or reraise
# Each mode that waits writes READY once its traps are installed, then runs a
# foreground or background /bin/sleep that the test interrupts with a signal.

mode=${1:?mode}
sleeper=

# The background /bin/sleep stands in for real work; every trap stops it so
# nothing outlives the probe.
start_sleeper() {
  /bin/sleep 5 &
  sleeper=$!
}

# shellcheck disable=SC2317 # invoked from trap actions
stop_sleeper() {
  if [ -n "$sleeper" ]; then
    { kill "$sleeper" && wait "$sleeper"; } 2>/dev/null
    sleeper=
  fi
}

case $mode in
  naive)
    ready=${2:?ready file}
    # The common mistake: clean up, then exit with the conventional number.
    trap 'stop_sleeper; printf "cleanup\n" >&2; exit 143' TERM
    : >"$ready"
    start_sleeper
    wait "$sleeper"
    ;;

  no-disarm)
    ready=${2:?ready file}
    # Re-raise without trap - EXIT: shells that run the EXIT trap on death by
    # an untrapped signal run the cleanup a second time.
    # shellcheck disable=SC2317 # invoked by the traps below
    cleanup() {
      stop_sleeper
      printf 'cleanup\n' >&2
    }
    trap cleanup EXIT
    trap 'cleanup; trap - TERM; kill -s TERM "$$"' TERM
    : >"$ready"
    start_sleeper
    wait "$sleeper"
    ;;

  after-kill)
    # The self-kill happens inside the TERM trap; anything printed after it
    # shows that the shell kept running after kill returned.
    trap 'trap - TERM; kill -s TERM "$$"; printf "after-kill\n"; exit 99' TERM
    kill -s TERM "$$"
    printf 'after-trap\n'
    exit 98
    ;;

  after-return)
    # The same self-kill, but the trap action ends without exit: a shell that
    # blocks the signal while its trap runs delivers it when the trap returns.
    trap 'trap - TERM; kill -s TERM "$$"' TERM
    kill -s TERM "$$"
    printf 'after-trap\n'
    exit 98
    ;;

  child-status)
    # A child dies of SIGTERM; the script passes its status on with exit.
    # shellcheck disable=SC2016 # $$ belongs to the child shell
    /bin/sh -c 'kill -s TERM "$$"'
    rc=$?
    printf 'rc=%s\n' "$rc"
    exit "$rc"
    ;;

  double)
    ready=${2:?ready file}
    # shellcheck disable=SC2317 # invoked by the TERM trap
    on_term() {
      trap '' TERM
      stop_sleeper
      printf 'cleanup-start\n' >&2
      kill -s TERM "$$" # a second TERM, as from an impatient supervisor
      printf 'cleanup-end\n' >&2
      trap - TERM
      kill -s TERM "$$"
      exit 143
    }
    trap on_term TERM
    : >"$ready"
    start_sleeper
    wait "$sleeper"
    ;;

  int-child)
    child_mode=${2:?exit or reraise}
    ready=${3:?ready file}
    case $child_mode in
      exit) trap 'exit 130' INT ;;
      reraise) trap 'trap - INT; kill -s INT "$$"; exit 130' INT ;;
      *)
        printf 'unknown child mode: %s\n' "$child_mode" >&2
        exit 2
        ;;
    esac
    : >"$ready"
    /bin/sleep 2
    ;;

  *)
    printf 'unknown mode: %s\n' "$mode" >&2
    exit 2
    ;;
esac
