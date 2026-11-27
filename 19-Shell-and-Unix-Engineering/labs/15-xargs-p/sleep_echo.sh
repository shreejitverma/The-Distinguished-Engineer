#!/bin/sh
# sleep_echo: sleep SECONDS in a real /bin/sleep, then print SECONDS.
# With "xargs -P2 -n1" fed 2 then 1, the output order is completion order: 1 then 2.
/bin/sleep "$1"
printf '%s\n' "$1"
