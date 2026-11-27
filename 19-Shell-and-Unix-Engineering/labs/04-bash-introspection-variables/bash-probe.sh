#!/bin/sh
# bash-probe.sh: run one bash-only probe from bash-probes.bash; prints "not-bash" under any other shell.
# The bash syntax lives in bash-probes.bash, sourced only after this guard, because some
# shells (mksh) parse a whole script before running any of it.
# Usage: bash-probe.sh MODE   (modes are listed at the end of bash-probes.bash)
if [ -z "${BASH_VERSION-}" ]; then
  echo not-bash
  exit 0
fi
case $0 in
  */*) here=${0%/*} ;;
  *) here=. ;;
esac
# shellcheck source=/dev/null # Bash-only code is checked separately in its own dialect
. "$here/bash-probes.bash"
