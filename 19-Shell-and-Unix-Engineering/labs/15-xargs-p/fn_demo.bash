#!/usr/bin/env bash
# fn_demo: hand a shell function to "xargs -P" jobs, with and without "export -f".
# Usage: fn_demo.bash exported|plain
# xargs exec()s its utility directly, so a function exists in a job only if the job is a bash that
# imports it from the environment (the exported-function variable BASH_FUNC_f%%).
# Prints: the sorted lines the jobs printed, then status=<xargs exit status>.
x=${XARGS:-xargs}
f() { printf 'got:%s\n' "$1"; }
case $1 in
  exported) export -f f ;;
  plain) ;;
  *) exit 2 ;;
esac
# shellcheck disable=SC2016 # $1 belongs to the inner bash -c, which is the point of the demo
printf 'a\nb\n' | "$x" -P 2 -n 1 bash -c 'f "$1"' _ 2>/dev/null | sort
echo "status=${PIPESTATUS[1]}"
