#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the hooks and tests by name
# A hook the suite does not define is an executable stub that shUnit2 puts first in PATH;
# a hook the suite does define is a shell function, and functions win the command search.
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

tearDown() { :; }

# report HOOK: print "HOOK: function", "HOOK: stub" (an executable shUnit2 created), or the path it resolves to.
report() {
  found=$(command -v "$1")
  case $found in
    "$1") echo "$1: function" ;;
    "${SHUNIT_TMPDIR%/tmp}/$1") echo "$1: stub" ;;
    *) echo "$1: $found" ;;
  esac
}

oneTimeSetUp() {
  report setUp
  report tearDown
  report oneTimeSetUp
  report oneTimeTearDown
  report suite
}

testNothing() { :; }

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
