#!/bin/sh
# worker.sh MODE SEM GATE PIDFILE - a job that holds a token and then blocks until it is killed.
# It records its pid in PIDFILE and blocks reading the empty FIFO GATE, which nobody ever writes.
# MODE A: the worker returns the token itself from an EXIT trap (cannot run after SIGKILL).
# MODE B: the worker returns nothing; its supervisor returns the token after the worker is gone.
set -u
mode=${1:?usage: worker.sh MODE SEM GATE PIDFILE}
sem=${2:?usage: worker.sh MODE SEM GATE PIDFILE}
gate=${3:?usage: worker.sh MODE SEM GATE PIDFILE}
pidfile=${4:?usage: worker.sh MODE SEM GATE PIDFILE}

exec 4<>"$gate" || exit 1
if [ "$mode" = A ]; then
  exec 3<>"$sem" || exit 1
  trap 'printf "\n" >&3' EXIT
fi
printf '%s\n' "$$" >"$pidfile"
# shellcheck disable=SC2034 # the value is unused, the read only blocks
read -r gate_token <&4
