#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the test functions by name through eval
# The smallest useful shUnit2 suite: every function whose name starts with "test" is a test.
# shUnit2 is a library: the last line sources it, and sourcing it runs the suite and then exits.
# zsh needs the shwordsplit option and the script name set before shUnit2 is sourced (README, "Zsh").
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

testEquality() {
  assertEquals 1 1
}

testStringsCompareAsStrings() {
  assertNotEquals 1 01
}

testNullness() {
  assertNull ''
  assertNotNull x
}

testCondition() {
  assertTrue '[ 2 -gt 1 ]'
}

testContains() {
  assertContains 'hello world' 'lo wo'
}

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
