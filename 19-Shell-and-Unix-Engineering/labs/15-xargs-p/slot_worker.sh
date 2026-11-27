#!/bin/sh
# slot_worker: claim the directory DIR/slot.$SLOT for one second; report a collision if it is taken.
# Usage: SLOT=<number> slot_worker.sh DIR   (xargs --process-slot-var=SLOT provides SLOT)
dir=$1
if ! mkdir "$dir/slot.$SLOT" 2>/dev/null; then
  printf 'COLLISION slot %s\n' "$SLOT" >>"$dir/collisions.log"
  exit 1
fi
printf '%s\n' "$SLOT" >>"$dir/slots.log"
/bin/sleep 1
rmdir "$dir/slot.$SLOT"
