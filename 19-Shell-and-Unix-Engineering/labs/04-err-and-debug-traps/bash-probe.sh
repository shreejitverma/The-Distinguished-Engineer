#!/bin/sh
# bash-probe.sh: bash-only ERR, DEBUG, and RETURN probes; prints "not-bash" under any other shell.
# The bash syntax lives in bash-probes.bash, sourced only after this guard, because some
# shells (mksh) parse a whole script before running any of it.
# Usage: bash-probe.sh MODE   (modes are listed in bash-probes.bash)
if [ -z "${BASH_VERSION-}" ]; then
  echo not-bash
  exit 0
fi
case $0 in
  */*) here=${0%/*} ;;
  *) here=. ;;
esac
. "$here/bash-probes.bash"
