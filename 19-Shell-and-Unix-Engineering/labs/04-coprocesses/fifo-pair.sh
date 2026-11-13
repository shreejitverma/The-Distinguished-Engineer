#!/bin/sh
# fifo-pair.sh REPLY MODE WORD...: a coprocess built from two FIFOs, in any POSIX shell.
#   REPLY  path of the compiled reply helper (answers each line with "ack LINE")
#   MODE   full (the C library default buffering) or line (line buffered)
# For each WORD it sends one line and prints "WORD -> FIRST", where FIRST is the first line that
# comes back: the answer, or "timeout" from a watchdog that writes to the same FIFO when no answer
# has arrived after 2 seconds. Then it closes the write end, prints every line that arrives before
# end of file as "after_eof=...", and prints the helper's exit status.
# Every FIFO open has a guaranteed peer, so nothing here can block forever.

filter=${1:?usage: fifo-pair.sh REPLY full|line WORD...}
mode=${2:?usage: fifo-pair.sh REPLY full|line WORD...}
shift 2

dir=$(mktemp -d) || exit 1
trap 'rm -rf "$dir"' EXIT
mkfifo "$dir/req" "$dir/rep" || exit 1

# The helper opens req for reading, then rep for writing; the two exec lines below are the
# peers of those opens, in the same order.
"$filter" "$mode" <"$dir/req" >"$dir/rep" &
helper=$!
exec 8>"$dir/req"
exec 9<"$dir/rep"

n=0
for word; do
  n=$((n + 1))
  printf '%s\n' "$word" >&8
  # The watchdog answers "timeout" only if the parent has not marked this request answered.
  (
    /bin/sleep 2
    [ -e "$dir/answered.$n" ] || echo timeout
  ) >"$dir/rep" &
  IFS= read -r first <&9 || first="(eof)"
  : >"$dir/answered.$n"
  printf '%s -> %s\n' "$word" "$first"
done

exec 8>&- # end of file for the helper: it exits, and fclose flushes what it still holds
late=
while IFS= read -r line <&9; do
  late="${late:+$late,}$line"
done
exec 9<&-
wait "$helper"
st=$?
wait # the watchdogs; a bare wait is enough because the helper status is already saved
echo "after_eof=$late"
echo "status=$st"
