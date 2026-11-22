#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the test functions by name through eval
# shUnit2 runs every test in the one shell that runs the suite: nothing is isolated between tests.
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

testA_defines_state() {
  leaked_variable=from-testA
  leaked_function() { echo from-testA; }
  assertEquals from-testA "$leaked_variable"
}

testB_sees_state_of_testA() {
  assertEquals from-testA "${leaked_variable-unset}"
  result=$(leaked_function)
  assertEquals from-testA "$result"
}

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
