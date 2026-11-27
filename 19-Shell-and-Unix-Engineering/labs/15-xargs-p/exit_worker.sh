#!/bin/sh
# exit_worker: a job whose behavior is chosen by its item, to probe how xargs handles failure.
# Usage: exit_worker.sh DIR ITEM
#   ok    exit 0
#   bad   exit 1
#   die   exit 255 (POSIX: tells xargs to stop reading input)
#   kill  kill itself with SIGKILL
#   slow  sleep 1 second, then record DIR/slow.done and exit 0
# Every call appends ITEM to DIR/seen.log first.
dir=$1
item=$2
printf '%s\n' "$item" >>"$dir/seen.log"
case $item in
  bad) exit 1 ;;
  die) exit 255 ;;
  kill) kill -9 "$$" ;;
  slow)
    /bin/sleep 1
    : >"$dir/slow.done"
    ;;
  *) ;;
esac
exit 0
