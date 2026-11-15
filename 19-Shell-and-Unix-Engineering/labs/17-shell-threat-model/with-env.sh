#!/bin/sh
# with-env.sh: start a command with extra environment entries, through env(1).
# The hostile values travel as arguments, so the shell that runs this script never imports them.
# Usage: with-env.sh [-i] NAME=VALUE... COMMAND [ARG...]
exec env "$@"
