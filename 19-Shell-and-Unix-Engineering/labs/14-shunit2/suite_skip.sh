#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the test functions by name through eval
# startSkipping turns every later assert and fail in the test into a counted no-op until the test ends.
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

testSkipsTheRest() {
  assertEquals 1 1
  startSkipping
  assertEquals 1 2
  assertTrue 1
  fail 'never recorded'
  isSkipping
  echo "isSkipping status: $?"
}

testSkippingEndsWithTheTest() {
  isSkipping
  echo "isSkipping status at start: $?"
  assertEquals 1 1
}

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
