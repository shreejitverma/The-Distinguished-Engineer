#!/bin/sh
# steamroot.sh: the reviewed version of steamroot-bad.sh.
# $0 has no slash when the script is started as `sh steamroot.sh`, so the directory is ".".
# cd gets no "--": it treats the operand "-" as OLDPWD even after "--" (XCU cd, APPLICATION USAGE),
# so every operand is made to start with "/" or "." instead.
# A failed cd stops the script, and ${STEAMROOT:?} stops an empty value before it reaches rm.
set -eu
set -o noglob
case $0 in
  /*) here=${0%/*} ;;
  */*) here=./${0%/*} ;;
  *) here=. ;;
esac
# An absolute $0 of the form /name has an empty ${0%/*}: that directory is the root.
[ -n "$here" ] || here=/
# shellcheck disable=SC1007 # CDPATH= cd is the POSIX idiom for an empty CDPATH for one command
STEAMROOT=$(CDPATH= cd -P "$here" && pwd) || exit 1
printf 'rm -rf %s\n' "${STEAMROOT:?}/"*
