#!/bin/sh
# installer-unwrapped.sh: a typical top-level installer, the shape that is unsafe to pipe into a shell.
# The shell runs each line as soon as it has read it, so a download cut off mid-file runs a prefix.
# remove() only prints the path it was given, standing in for a destructive command.
set -eu
remove() {
  printf 'REMOVE %s\n' "$1"
}
echo "install: begin"
remove /opt/tool/cache
echo "install: done"
