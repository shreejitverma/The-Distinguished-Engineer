#!/bin/sh
# stdin-steal.sh MODE SCRATCH: loop over a three-line file with "while read" and start one "cat" per line.
#   bg:     cat is a background job; POSIX 2.9.3.1 gives it /dev/null as stdin when job control is off.
#           The loop then waits 1 s, so a job that kept the loop's stdin has time to drain it.
#   bgnull: the same, with an explicit </dev/null on the job (the portable fix).
#   fg:     cat runs in the foreground and drains the loop's own input.
# Prints "lines=N", the number of times the loop body ran. SCRATCH is the three-line file to create.
case $1 in
  bg | bgnull | fg) ;;
  *)
    echo "usage: stdin-steal.sh bg|bgnull|fg SCRATCH" >&2
    exit 2
    ;;
esac
printf '%s\n' a b c >"$2"
n=0
while read -r _line; do
  n=$((n + 1))
  case $1 in
    bg)
      /bin/cat >/dev/null &
      /bin/sleep 1
      ;;
    bgnull)
      /bin/cat >/dev/null </dev/null &
      /bin/sleep 1
      ;;
    fg) /bin/cat >/dev/null ;;
    *) exit 2 ;;
  esac
done <"$2"
wait
echo "lines=$n"
