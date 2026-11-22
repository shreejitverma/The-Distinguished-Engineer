#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the test functions by name through eval
# In shUnit2 2.1.8, assertNull builds "[ -z '$1' ]" and evals it, so a value that contains a single quote
# is code. LAB_MARKER names a file the injected command creates; a harmless stand-in for any command.
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

testInjectedValuePassesAssertNull() {
  assertNull "x'; touch \"\$LAB_MARKER\"; echo '"
}

testPlainNonNullValueFailsAssertNull() {
  assertNull x
}

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
