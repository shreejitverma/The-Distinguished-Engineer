#!/bin/sh
# fake_shell.sh -c STRING: stand-in for $SHELL that reports the string it was asked to run.
printf 'fake shell got: %s %s\n' "$1" "$2"
