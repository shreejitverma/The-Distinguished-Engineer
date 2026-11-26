#!/usr/bin/env bash
# stack.bash: show FUNCNAME, BASH_SOURCE, BASH_LINENO and caller from three frames deep.
# h is defined in stack-lib.bash, which this script sources; run it by absolute path.
here=${BASH_SOURCE[0]%/*}
. "$here/stack-lib.bash"
g() {
  h
}
f() {
  g
}
# shellcheck disable=SC2312 # caller's output, not its status, is what this line shows
echo "top: FUNCNAME=${FUNCNAME-unset} caller=$(caller)"
f
shopt -s extdebug
short "$(declare -F h)"
