#!/bin/sh
# half-line.sh ID: write "ID-" with no newline, sleep, then write "ID" and a newline.
# The first write is half a line, so how parallel buffers output decides whether lines mix.
printf '%s-' "$1"
/bin/sleep 2
printf '%s\n' "$1"
