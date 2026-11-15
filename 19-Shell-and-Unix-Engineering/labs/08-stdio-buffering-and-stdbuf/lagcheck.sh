#!/bin/sh
# lagcheck: does a pipeline stage forward a line while its input is still open?
# Usage: lagcheck.sh PIPEPROBE builtin
#        lagcheck.sh PIPEPROBE cmd COMMAND [ARG...]
# Runs   pipeprobe feed CTL | STAGE | pipeprobe watch CTL 2000
# where STAGE is a read/printf loop in the shell running this script (builtin)
# or COMMAND (cmd), and prints the watcher's "early=N total=M" line.
# early=6 means the stage forwarded "hello\n" at once; early=0 means it held
# the line until its input closed. The FIFO lives in a mktemp -d directory
# under TMPDIR (the tests point TMPDIR at their own scratch directory).
set -u
probe=$1
mode=$2
shift 2

dir=$(mktemp -d "${TMPDIR:-/tmp}/lagcheck.XXXXXX") || exit 1
ctl=$dir/ctl
if ! mkfifo "$ctl"; then
  rm -rf "$dir"
  exit 1
fi

relay() {
  while IFS= read -r line; do
    printf '%s\n' "$line"
  done
}

case $mode in
  builtin)
    "$probe" feed "$ctl" | relay | "$probe" watch "$ctl" 2000
    ;;
  cmd)
    "$probe" feed "$ctl" | ${1+"$@"} | "$probe" watch "$ctl" 2000
    ;;
  *)
    echo "lagcheck: unknown mode $mode" >&2
    rm -rf "$dir"
    exit 2
    ;;
esac
status=$?
rm -rf "$dir"
exit "$status"
