#!/bin/sh
# job-events.sh EVENTS ID [SECONDS]: record "S ID" in EVENTS, sleep, then record "E ID".
# Each record is one short append, so concurrent jobs interleave whole lines in real-time order.
printf 'S %s\n' "$2" >>"$1"
/bin/sleep "${3:-2}"
printf 'E %s\n' "$2" >>"$1"
