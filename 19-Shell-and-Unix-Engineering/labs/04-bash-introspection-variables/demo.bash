#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR # die, from stacktrace.bash, reports the stack and exits 3
case ${BASH_SOURCE[0]} in
  */*) here=${BASH_SOURCE[0]%/*} ;;
  *) here=. ;;
esac
. "$here/stacktrace.bash"
read_config() {
  [ -r "$1" ] || die "cannot read config: $1" 3
}
load() {
  read_config "$1"
}
load /nonexistent/app.conf
echo "not reached"
