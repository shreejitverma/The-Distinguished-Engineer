#!/bin/sh
# probe-env.sh - report the environment a script under characterization really sees.
printf 'HOME=%s\n' "${HOME-unset}"
printf 'TMPDIR=%s\n' "${TMPDIR-unset}"
printf 'LC_ALL=%s\n' "${LC_ALL-unset}"
printf 'TZ=%s\n' "${TZ-unset}"
printf 'USER=%s\n' "${USER-unset}"
printf 'leak=%s\n' "${CHAR_LEAK-unset}"
here=$(pwd -P)
printf 'pwd=%s\n' "$here"
bare=$(date +%Y)
printf 'bare date: %s\n' "$bare"
absolute=$(/bin/date +%Y)
case $absolute in
  [0-9][0-9][0-9][0-9]) echo "absolute date: a four digit year" ;;
  *) echo "absolute date: unexpected $absolute" ;;
esac
