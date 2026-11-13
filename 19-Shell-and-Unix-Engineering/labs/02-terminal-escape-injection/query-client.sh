#!/bin/sh
# query-client.sh COUNT: a program that asks its terminal a question and then
# reads the answer as ordinary input.  It must run with a real terminal on
# stdin and stdout (inject_probe term provides one).
#
# It writes CSI 21 t (report window title), then reads COUNT lines in canonical
# mode and prints each one.  The point of the lab is that the terminal's reply
# arrives on this program's input exactly as if it had been typed, and that a
# line terminator inside the reply completes a line with no human keystroke.
set -eu

count=${1:-1}

# Ask the terminal to report its title.  A real xterm answers OSC l <title> ST.
printf '\033[21t'

i=0
while [ "$i" -lt "$count" ]; do
  if IFS= read -r line; then
    rc=0
  else
    rc=$?
  fi
  printf 'INPUT=[%s] rc=%d\n' "$line" "$rc"
  i=$((i + 1))
done
