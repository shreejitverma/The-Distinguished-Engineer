#!/bin/sh
# jobs-count.sh: start a 3 s and a 1 s background sleep, count them with the usual jobs idioms
# before and after the short one has exited (the true running count is 2, then 1).
# Prints "before rp=A p=B after rp=C p=D"; each is the line count of "jobs -rp" or "jobs -p" run in a
# command substitution, with stderr merged so an unsupported option counts as a line.
/bin/sleep 3 &
/bin/sleep 1 &
count() {
  # $(...) runs in a subshell in every shell, which is the point: some give it the job table.
  "$@" 2>&1 | wc -l | tr -d ' '
}
rp1=$(count jobs -rp)
p1=$(count jobs -p)
/bin/sleep 2
rp2=$(count jobs -rp)
p2=$(count jobs -p)
echo "before rp=$rp1 p=$p1 after rp=$rp2 p=$p2"
wait
