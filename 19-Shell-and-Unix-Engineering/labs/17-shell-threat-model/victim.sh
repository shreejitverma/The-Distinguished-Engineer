#!/bin/sh
# victim.sh: an innocent script that runs one external utility and prints its result.
# The tests start it with hostile environment variables to show which shells run attacker
# code before this file's first line executes.
# Expected output with a clean environment: body y
printf 'body %s\n' "$(basename /x/y)"
