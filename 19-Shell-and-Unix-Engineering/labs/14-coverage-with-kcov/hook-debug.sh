# shellcheck shell=bash
# The DEBUG method: kcov writes this file as bash-helper-debug-trap.sh.
# The trap runs before every simple command and writes the marker to the trace descriptor.
# BASH_ENV is unset so child shells do not source this file again.
set -o functrace
# shellcheck disable=SC2016 # the trap body expands when the trap fires, not now
trap 'echo "kcov@${BASH_SOURCE}@${LINENO}@" >&$KCOV_BASH_XTRACEFD' DEBUG
unset BASH_ENV
