#!/bin/sh
# fsize.sh FILE: print the size of FILE in bytes as a bare integer, or -1 when it does not exist.
if [ -e "$1" ]; then
  wc -c <"$1" | tr -d '[:space:]'
  echo
else
  echo -1
fi
