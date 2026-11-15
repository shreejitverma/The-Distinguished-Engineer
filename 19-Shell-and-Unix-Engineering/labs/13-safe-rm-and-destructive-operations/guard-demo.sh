#!/bin/sh
# guard-demo.sh: show that ${var:?} stops a non-interactive shell before a destructive command.
# Usage: guard-demo.sh DIR
# It prints the rm command it would run and never removes anything.
dir=$1
printf 'would run: rm -rf -- %s\n' "${dir:?refusing: empty dir}/cache"
printf 'after\n'
