#!/bin/sh
# shellcheck disable=SC2317,SC2329 # shUnit2 calls the test functions by name through eval
# The line-number macro: ${_ASSERT_EQUALS_} expands to "eval assertEquals --lineno $LINENO", so each
# argument is parsed twice and a message with a space must be quoted twice.
# shellcheck disable=SC2034 # SHUNIT_PARENT is read by shUnit2 when it is sourced
[ -n "${ZSH_VERSION:-}" ] && {
  emulate zsh
  setopt shwordsplit
  SHUNIT_PARENT=$0
}

testMacroAddsALineNumber() {
  # shellcheck disable=SC2086 # the macro is an eval prefix and must stay unquoted
  ${_ASSERT_EQUALS_} '"two words"' x y # line-marker
}

testUnquotedMessageWithASpaceBreaksTheArity() {
  # shellcheck disable=SC2086 # the macro is an eval prefix and must stay unquoted
  ${_ASSERT_EQUALS_} 'two words' x y
}

# shellcheck source=/dev/null
. "$SHUNIT2_LIB"
