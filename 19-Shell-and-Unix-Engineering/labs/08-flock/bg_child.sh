#!/bin/sh
# bg_child.sh PIDFILE: start a background /bin/sleep that inherits every open descriptor,
# write its pid to PIDFILE, and exit at once (a "daemon" started under a lock).
/bin/sleep 5 &
echo "$!" >"$1"
