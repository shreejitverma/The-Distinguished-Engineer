#!/bin/sh
# cleanup-template: the cleanup-and-re-raise pattern for POSIX sh.
# Usage: cleanup-template.sh BASE_DIR READY_FILE SECONDS [CODE]
# Creates a scratch directory under BASE_DIR, starts a background worker
# (/bin/sleep SECONDS), writes "SCRATCH WORKER_PID" to READY_FILE, waits for
# the worker, and exits with CODE (default 0).
# On EXIT, HUP, INT, or TERM it stops the worker and removes the scratch
# directory exactly once; after a signal it re-raises that signal, so the
# caller's wait status says "killed by SIGTERM", not "exited 143".

base=${1:?base dir}
ready=${2:?ready file}
seconds=${3:?seconds}
code=${4:-0}

scratch=
worker=

# shellcheck disable=SC2329 # invoked from the trap actions below
cleanup() {
  # Disarm first: a second signal must not re-enter, and EXIT must not rerun.
  trap '' HUP INT TERM
  trap - EXIT
  if [ -n "$worker" ]; then
    { kill "$worker" && wait "$worker"; } 2>/dev/null
    worker=
  fi
  if [ -n "$scratch" ]; then
    rm -rf -- "$scratch"
    printf 'cleanup: removed %s\n' "$scratch" >&2
    scratch=
  fi
}

# shellcheck disable=SC2329 # invoked from the trap actions below
on_exit() {
  rc=$?
  cleanup
  exit "$rc"
}

# shellcheck disable=SC2329 # invoked from the trap actions below
on_signal() {
  cleanup
  trap - "$1"
  kill -s "$1" "$$"
  # Reached only if the self-kill did not end the process (PID 1 in a
  # container, or a shell that keeps catching the signal): exit 128+N.
  exit $((128 + $2))
}

trap on_exit EXIT
trap 'on_signal HUP 1' HUP
trap 'on_signal INT 2' INT
trap 'on_signal TERM 15' TERM

scratch=$(mktemp -d "$base/job.XXXXXX") || exit 1
/bin/sleep "$seconds" &
worker=$!
printf '%s %s\n' "$scratch" "$worker" >"$ready"
wait "$worker"
worker=
exit "$code"
