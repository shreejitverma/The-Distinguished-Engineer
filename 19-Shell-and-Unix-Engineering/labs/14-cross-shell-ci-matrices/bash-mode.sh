#!/bin/sh
# bash-mode.sh: report whether the bash running this script is in POSIX mode (run it under bash).
# Usage: bash bash-mode.sh   or, through a link named sh, sh bash-mode.sh
# shellcheck disable=SC3062 # this probe runs under bash, where test -o exists
if [ -z "${BASH_VERSION-}" ]; then
  echo not-bash
elif [ -o posix ]; then
  echo posix=on
else
  echo posix=off
fi
