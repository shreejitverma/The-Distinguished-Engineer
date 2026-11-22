#!/bin/sh
# report.sh STATUS: print a report with empty lines on stdout, a warning on stderr, exit STATUS.
# The probe for what bats `run` captures: stdout and stderr in order, empty lines, trailing newlines.
printf 'alpha\n\nbeta\n\n\n'
printf 'warn: disk almost full\n' >&2
exit "${1:-0}"
