#!/bin/sh
# lines_demo: eight parallel jobs each print 100 lines of 100 bytes to a shared pipe.
# Usage: lines_demo.sh
# Prints: lines=<total lines> intact=<lines that are a whole, unmixed record>
# Each line is a single write(2) of 100 bytes, below the 512-byte minimum PIPE_BUF, so POSIX
# makes it atomic: no line is torn, but the order of lines from different jobs is not defined.
here=$(cd "$(dirname "$0")" && pwd)
x=${XARGS:-xargs}
out=$(seq 1 8 | "$x" -P 8 -n 1 "$here/line_writer.sh")
lines=$(printf '%s\n' "$out" | wc -l)
intact=$(printf '%s\n' "$out" | grep -c -E '^w[0-9] x{96}$')
printf 'lines=%s intact=%s\n' "$((lines))" "$((intact))"
