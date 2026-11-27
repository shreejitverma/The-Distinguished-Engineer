#!/bin/sh
# maxrun.sh EVENTS: print the most workers that were running at once, from worker.sh records.
awk '$1 == "+" { run++; if (run > max) max = run } $1 == "-" { run-- } END { print max + 0 }' "$1"
