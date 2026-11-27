#!/bin/sh
# wait-probe.sh: what `wait` returns when a trapped signal interrupts it, against a child's own death.
# Usage: wait-probe.sh MODE WORKDIR
#   interrupted  a signal sent to this shell interrupts `wait CHILD`; the child keeps running
#   killed       the child is killed by SIGTERM, so `wait CHILD` returns its signal status
# Output: first_wait status=CLASS child_alive=yes|no trap_ran=0|1, then (interrupted only)
#         second_wait status=CLASS, where CLASS is gt128, zero, or other:STATUS.
if [ "$#" -ne 2 ]; then
  echo "usage: wait-probe.sh interrupted|killed WORKDIR" >&2
  exit 2
fi
mode=$1
work=$2
flag=0

classify() {
  if [ "$1" -gt 128 ]; then
    echo gt128
  elif [ "$1" -eq 0 ]; then
    echo zero
  else
    echo "other:$1"
  fi
}

case $mode in
  interrupted)
    trap 'flag=1' TERM
    /bin/sleep 3 &
    child=$!
    : >"$work/ready"
    wait "$child"
    rc=$?
    ;;
  killed)
    /bin/sleep 3 &
    child=$!
    /bin/sleep 1 # let the child exec: a signal between fork and exec is lost in some shells
    kill -s TERM "$child"
    wait "$child"
    rc=$?
    ;;
  *)
    echo "wait-probe.sh: unknown mode $mode" >&2
    exit 2
    ;;
esac

if kill -0 "$child" 2>/dev/null; then
  alive=yes
else
  alive=no
fi
cls=$(classify "$rc")
printf 'first_wait status=%s child_alive=%s trap_ran=%s\n' "$cls" "$alive" "$flag"

if [ "$mode" = interrupted ]; then
  wait "$child"
  rc=$?
  cls=$(classify "$rc")
  printf 'second_wait status=%s\n' "$cls"
fi
