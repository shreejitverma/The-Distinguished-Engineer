#!/bin/sh
# grandchild.sh PIDFILE: start a 30 second background /bin/sleep, record its pid in PIDFILE, wait for it.
# A job that has children of its own, to see whether a kill reaches them.
/bin/sleep 30 &
printf '%s\n' "$!" >"$1"
wait
