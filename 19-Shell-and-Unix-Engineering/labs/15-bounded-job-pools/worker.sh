#!/bin/sh
# worker.sh EVENTS ID SECS [EXIT]: append "+ ID PID" to EVENTS, run for SECS, append "- ID", exit with EXIT.
# SECS=hold: append "+" and then exec a 30 s sleep, so PID names the sleeping process and a kill reaches it.
# Each record is one short append, so concurrent workers interleave whole lines in real-time order.
printf '%s %s %s\n' + "$2" "$$" >>"$1"
if [ "$3" = hold ]; then
  exec /bin/sleep 30
fi
/bin/sleep "$3"
printf '%s %s\n' - "$2" >>"$1"
exit "${4:-0}"
