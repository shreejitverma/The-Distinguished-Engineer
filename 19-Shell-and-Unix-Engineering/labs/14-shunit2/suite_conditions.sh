#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the test functions by name through eval
# assertTrue and assertFalse take either a number (an exit status) or a string that is eval'd in a subshell.
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

testConditionRunsInSubshell() {
  marker=outer
  assertTrue 'marker=inner; [ 1 -eq 1 ]'
  assertEquals outer "$marker"
}

testNumbersAreExitStatuses() {
  assertTrue 0
  assertFalse 1
  true
  exit_status=$?
  assertTrue "$exit_status"
}

testNumberOneFailsAssertTrue() {
  assertTrue 1
}

testEmptyConditionFailsBothAsserts() {
  assertTrue ''
  assertFalse ''
}

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
