#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the hooks and tests by name
# Order of the hooks: oneTimeSetUp, then setUp + test + tearDown per test, then oneTimeTearDown.
# shUnit2 itself prints the test name after setUp and before the test body.
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

oneTimeSetUp() { echo 'hook: oneTimeSetUp'; }
oneTimeTearDown() { echo 'hook: oneTimeTearDown'; }
setUp() { echo 'hook: setUp'; }
tearDown() { echo 'hook: tearDown'; }

testOne() { echo 'body: one'; }
testTwo() { echo 'body: two'; }

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
