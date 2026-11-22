#!/bin/sh
# max-concurrency.sh EVENTS: print the most jobs that were running at once, from job-events.sh records.
awk '$1 == "S" { run++; if (run > max) max = run } $1 == "E" { run-- } END { print max + 0 }' "$1"
