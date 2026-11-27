#!/bin/sh
# borrow.sh - take one token from a GNU make style FIFO jobserver named in MAKEFLAGS, hold it, return it.
# The protocol: MAKEFLAGS carries --jobserver-auth=fifo:PATH; every token is one byte (make writes "+");
# take a token by reading exactly one byte; return the same byte by writing it back.
# A job server token is not a newline, so the shell builtin "read" cannot take one: it would
# consume bytes until a newline arrives. dd with bs=1 count=1 reads exactly one byte.
# Prints "token=<byte>", "held=<queued>" and "returned=<queued>" when FIFOTOOL names fifotool.
set -u
fifo_path=$(printf '%s\n' "${MAKEFLAGS-}" | sed -n 's/.*--jobserver-auth=fifo:\([^ ]*\).*/\1/p') || exit 1
[ -n "$fifo_path" ] || {
  echo "borrow.sh: no --jobserver-auth=fifo: in MAKEFLAGS" >&2
  exit 1
}
# shellcheck disable=SC2094 # one FIFO, opened once for reading and once for writing, is the protocol
exec 3<"$fifo_path" 4>"$fifo_path" || exit 1
token=$(dd bs=1 count=1 <&3 2>/dev/null) || exit 1
[ -n "$token" ] || {
  echo "borrow.sh: read no token" >&2
  exit 1
}
printf 'token=%s\n' "$token"
if [ -n "${FIFOTOOL-}" ]; then
  held=$("$FIFOTOOL" count "$fifo_path") || exit 1
  printf 'held=%s\n' "$held"
fi
printf '%s' "$token" >&4
if [ -n "${FIFOTOOL-}" ]; then
  back=$("$FIFOTOOL" count "$fifo_path") || exit 1
  printf 'returned=%s\n' "$back"
fi
