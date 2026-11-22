#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the test functions by name through eval
# Behaviors of the 2.1.8 assertions that surprise: a multi-line needle, an empty needle, backslashes in messages.
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

testNeedleWithNewlineMatchesEitherLine() {
  assertContains foo 'bar
foo'
}

testEmptyNeedleIsFound() {
  assertContains abc ''
}

testEmptyNeedleIsNeverAbsent() {
  assertNotContains abc ''
}

testBackslashEscapesInMessagesAreInterpreted() {
  assertEquals 'C:\temp' 'C:\tmp'
}

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
