#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the test functions by name through eval
# How shUnit2 counts: "failures" counts failed asserts, plus one for each test that returns non-zero.
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

testAssertFailsButTestReturnsZero() {
  assertEquals 1 2
  return 0
}

testAssertsPassButLastCommandFails() {
  assertEquals 1 1
  false
}

testTwoFailingAsserts() {
  assertEquals first 1 2
  assertEquals second a b
}

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
