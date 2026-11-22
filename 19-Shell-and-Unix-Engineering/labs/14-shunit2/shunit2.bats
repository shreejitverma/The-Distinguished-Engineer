#!/usr/bin/env bats
# Lab for the shUnit2 note: the suites next to this file are real shUnit2 2.1.8 suites, run under the shell under test.
#
# vendor/shunit2-2.1.8.lib is the upstream v2.1.8 script (see vendor/PROVENANCE.txt).
# Every suite is started through run_lab, so LAB_SHELL decides which shell runs shUnit2 and the code under test.
# The mktemp shim set up below keeps shUnit2's temporary directory inside BATS_TEST_TMPDIR on every host.

setup() {
  load ../lib/lab
  export SHUNIT_COLOR=none
  export SHUNIT2_LIB="$BATS_TEST_DIRNAME/vendor/shunit2-2.1.8.lib"
  export LAB_TMP="$BATS_TEST_TMPDIR/shunit-tmp"
  export LAB_MARKER="$BATS_TEST_TMPDIR/marker"
  mkdir -p "$LAB_TMP" "$BATS_TEST_TMPDIR/bin"
  cat >"$BATS_TEST_TMPDIR/bin/mktemp" <<'EOF'
#!/bin/sh
# Stand-in for "mktemp -dqt shunit.XXXXXX": the same contract, but rooted in LAB_TMP.
n=0
while ! mkdir "$LAB_TMP/shunit.$$.$n" 2>/dev/null; do
  n=$((n + 1))
done
printf '%s\n' "$LAB_TMP/shunit.$$.$n"
EOF
  chmod +x "$BATS_TEST_TMPDIR/bin/mktemp"
  PATH="$BATS_TEST_TMPDIR/bin:$PATH"
}

# suite SCRIPT [ARG...]: run an shUnit2 suite under the shell under test.
# GNU grep 3.8 and later makes egrep print an obsolescence warning on stderr; shUnit2 2.1.8 calls egrep.
# The warning is not part of what these tests measure, so it is dropped from $output.
suite() {
  local line filtered=''
  run_lab "$@"
  while IFS= read -r line; do
    case $line in
      'egrep: warning:'*) ;;
      *) filtered=${filtered:+$filtered$'\n'}$line ;;
    esac
  done <<<"$output"
  output=$filtered
}

# gen_suite NAME [raw]: write a suite into BATS_TEST_TMPDIR from stdin and set SUITE to its path.
# The suite gets a shebang, the zsh prologue (unless "raw"), the body from stdin, and the line that sources shUnit2.
gen_suite() {
  SUITE="$BATS_TEST_TMPDIR/$1"
  {
    if [ "${2-}" = raw ]; then
      echo '#!/bin/sh'
    else
      cat <<'EOF'
#!/bin/sh
[ -n "${ZSH_VERSION:-}" ] && { emulate zsh; setopt shwordsplit; SHUNIT_PARENT=$0; }
EOF
    fi
    cat
    cat <<'EOF'
. "$SHUNIT2_LIB"
EOF
  } >"$SUITE"
  chmod +x "$SUITE"
}

# count_shunit_dirs: set SHUNIT_DIRS to how many shUnit2 temporary directories remain in LAB_TMP.
count_shunit_dirs() {
  local d
  SHUNIT_DIRS=0
  for d in "$LAB_TMP"/shunit.*; do
    [ -d "$d" ] && SHUNIT_DIRS=$((SHUNIT_DIRS + 1))
  done
  return 0
}

# --- the shape of a run ---------------------------------------------------------------------

@test "a passing suite lists its tests, counts them, reports OK, and exits 0" {
  suite "$BATS_TEST_DIRNAME/suite_basic.sh"
  expected=$(
    cat <<'EOF'
testEquality
testStringsCompareAsStrings
testNullness
testCondition
testContains

Ran 5 tests.

OK
EOF
  )
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

@test "failures counts failed asserts plus one for every test that returns non-zero" {
  suite "$BATS_TEST_DIRNAME/suite_status.sh"
  expected=$(
    cat <<'EOF'
testAssertFailsButTestReturnsZero
ASSERT:expected:<1> but was:<2>
testAssertsPassButLastCommandFails
shunit2:ERROR testAssertsPassButLastCommandFails() returned non-zero return code.
testTwoFailingAsserts
ASSERT:first expected:<1> but was:<2>
ASSERT:second expected:<a> but was:<b>
shunit2:ERROR testTwoFailingAsserts() returned non-zero return code.

Ran 3 tests.

FAILED (failures=5)
EOF
  )
  [ "$status" -eq 1 ]
  [ "$output" = "$expected" ]
}

@test "the hooks run in a fixed order and the exit trap runs tearDown and oneTimeTearDown a second time" {
  suite "$BATS_TEST_DIRNAME/suite_hooks.sh"
  expected=$(
    cat <<'EOF'
hook: oneTimeSetUp
hook: setUp
testOne
body: one
hook: tearDown
hook: setUp
testTwo
body: two
hook: tearDown
hook: oneTimeTearDown

Ran 2 tests.

OK
hook: tearDown
hook: oneTimeTearDown
EOF
  )
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

@test "SHUNIT_TEST_PREFIX is printed in front of each test name" {
  SHUNIT_TEST_PREFIX='== ' suite "$BATS_TEST_DIRNAME/suite_hooks.sh"
  [ "$status" -eq 0 ]
  [[ $output == *$'\nhook: setUp\n== testOne\nbody: one\n'* ]]
  [[ $output == *$'\nhook: setUp\n== testTwo\nbody: two\n'* ]]
}

@test "an undefined hook is an executable stub first in PATH and a defined hook is a function" {
  suite "$BATS_TEST_DIRNAME/suite_stubs.sh"
  expected=$(
    cat <<'EOF'
setUp: stub
tearDown: function
oneTimeSetUp: function
oneTimeTearDown: stub
suite: stub
testNothing

Ran 1 test.

OK
EOF
  )
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

@test "tests share one shell: variables and functions leak from one test to the next" {
  suite "$BATS_TEST_DIRNAME/suite_leak.sh"
  expected=$(
    cat <<'EOF'
testA_defines_state
testB_sees_state_of_testA

Ran 2 tests.

OK
EOF
  )
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

# --- discovery and selection ----------------------------------------------------------------

@test "tests are found by a text search of the script, not by asking the shell" {
  export LAB_INCLUDE="$BATS_TEST_TMPDIR/included.inc"
  cat >"$LAB_INCLUDE" <<'EOF'
testFromInclude() { echo SHOULD-NOT-RUN; }
EOF
  gen_suite discovery.sh <<'EOF'
. "$LAB_INCLUDE"
oneTimeSetUp() {
  if type testFromInclude >/dev/null 2>&1; then
    echo 'testFromInclude is defined'
  fi
}
testInScript() { assertTrue 0; }
testdirHelper() { assertTrue 0; }
  testIndented() { assertTrue 0; }
testSpaced () { assertTrue 0; }
# testCommented() { echo SHOULD-NOT-RUN; }
EOF
  suite "$SUITE"
  expected=$(
    cat <<'EOF'
testFromInclude is defined
testInScript
testdirHelper
testIndented
testSpaced

Ran 4 tests.

OK
EOF
  )
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

@test "a suite in which no function is named test* runs zero tests and still reports OK with status 0" {
  gen_suite zero.sh <<'EOF'
Test_Upper_Case_Prefix() { assertEquals 1 2; }
check_something() { assertEquals 1 2; }
EOF
  suite "$SUITE"
  [ "$status" -eq 0 ]
  [[ $output == *$'Ran 0 tests.\n\nOK' ]]
  [[ $output != *ASSERT* ]]
}

@test "arguments after -- select the tests, in the order given, including names that do not start with test" {
  suite "$BATS_TEST_DIRNAME/suite_basic.sh" -- testContains testEquality
  expected=$(
    cat <<'EOF'
testContains
testEquality

Ran 2 tests.

OK
EOF
  )
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

@test "test names without the -- marker are read as a script name and are fatal" {
  suite "$BATS_TEST_DIRNAME/suite_basic.sh" testContains
  [ "$status" -eq 2 ]
  [ "$output" = "shunit2:FATAL unable to read from testContains" ]
}

@test "a selected function that does not exist fails that test and the run continues" {
  suite "$BATS_TEST_DIRNAME/suite_basic.sh" -- testEquality nosuchtest
  [ "$status" -eq 1 ]
  [[ $output == *$'testEquality\nnosuchtest\n'* ]]
  [[ $output == *"shunit2:ERROR nosuchtest() returned non-zero return code."* ]]
  [[ $output == *$'Ran 2 tests.\n\nFAILED (failures=1)' ]]
}

@test "a suite() function, the deprecated way, fixes the tests and their order" {
  gen_suite suitefn.sh <<'EOF'
testA() { echo ran-A; }
testB() { echo ran-B; }
testC() { echo ran-C; }
suite() {
  suite_addTest testB
  suite_addTest testA
}
EOF
  suite "$SUITE"
  expected=$(
    cat <<'EOF'
testB
ran-B
testA
ran-A

Ran 2 tests.

OK
EOF
  )
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

@test "standalone mode sources the test file for you; native zsh and zsh emulating sh refuse it" {
  suite "$SHUNIT2_LIB" "$BATS_TEST_DIRNAME/standalone_tests.sh"
  case $(lab_shell_family) in
    bash | dash | ksh93 | mksh | yash | posh | busybox)
      expected=$(
        cat <<'EOF'
testOne
ran: one
testTwo
ran: two

Ran 2 tests.

OK
EOF
      )
      [ "$status" -eq 0 ]
      [ "$output" = "$expected" ]
      suite "$SHUNIT2_LIB" "$BATS_TEST_DIRNAME/standalone_tests.sh" testTwo otherFunction
      [ "$status" -eq 0 ]
      [[ $output == *$'testTwo\nran: two\notherFunction\nran: other\n\nRan 2 tests.'* ]]
      ;;
    zsh)
      [ "$status" -eq 2 ]
      [[ $output == *"command not found: echo -e"* ]]
      ;;
    zsh-sh)
      [ "$status" -eq 2 ]
      [[ $output == *"shunit2:FATAL zsh shwordsplit option is required for proper operation"* ]]
      ;;
    *) false ;;
  esac
}

# --- zsh ------------------------------------------------------------------------------------

@test "a suite without the zsh prologue runs as written everywhere except zsh" {
  gen_suite raw.sh raw <<'EOF'
testA() { assertEquals 1 1; }
EOF
  suite "$SUITE"
  case $(lab_shell_family) in
    bash | dash | ksh93 | mksh | yash | posh | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = $'testA\n\nRan 1 test.\n\nOK' ]
      ;;
    zsh)
      # shwordsplit is off, so shUnit2's own unquoted "echo -e" is one word and the fatal message is lost.
      [ "$status" -eq 2 ]
      [[ $output == *"command not found: echo -e"* ]]
      ;;
    zsh-sh)
      # setopt lists only options that differ from the emulation's defaults, and sh emulation turns shwordsplit on.
      [ "$status" -eq 2 ]
      [ "$output" = "shunit2:FATAL zsh shwordsplit option is required for proper operation" ]
      ;;
    *) false ;;
  esac
}

# --- what the asserts really do -------------------------------------------------------------

@test "assertTrue and assertFalse take an exit status or a string that is eval'd in a subshell" {
  suite "$BATS_TEST_DIRNAME/suite_conditions.sh"
  expected=$(
    cat <<'EOF'
testConditionRunsInSubshell
testNumbersAreExitStatuses
testNumberOneFailsAssertTrue
ASSERT:
shunit2:ERROR testNumberOneFailsAssertTrue() returned non-zero return code.
testEmptyConditionFailsBothAsserts
ASSERT:
ASSERT:
shunit2:ERROR testEmptyConditionFailsBothAsserts() returned non-zero return code.

Ran 4 tests.

FAILED (failures=5)
EOF
  )
  [ "$status" -eq 1 ]
  [ "$output" = "$expected" ]
}

@test "assertNull evals its value: a quote in the value runs code and can make the assert pass" {
  [ ! -e "$LAB_MARKER" ]
  suite "$BATS_TEST_DIRNAME/suite_injection.sh"
  expected=$(
    cat <<'EOF'
testInjectedValuePassesAssertNull
testPlainNonNullValueFailsAssertNull
ASSERT:
shunit2:ERROR testPlainNonNullValueFailsAssertNull() returned non-zero return code.

Ran 2 tests.

FAILED (failures=2)
EOF
  )
  [ "$status" -eq 1 ]
  [ "$output" = "$expected" ]
  [ -e "$LAB_MARKER" ]
}

@test "assertContains treats a newline in the needle as two patterns and an empty needle as found" {
  suite "$BATS_TEST_DIRNAME/suite_quirks.sh"
  local tab
  tab=$(printf '\t')
  expected=$(
    cat <<EOF
testNeedleWithNewlineMatchesEitherLine
testEmptyNeedleIsFound
testEmptyNeedleIsNeverAbsent
ASSERT:Found
shunit2:ERROR testEmptyNeedleIsNeverAbsent() returned non-zero return code.
testBackslashEscapesInMessagesAreInterpreted
ASSERT:expected:<C:${tab}emp> but was:<C:${tab}mp>
shunit2:ERROR testBackslashEscapesInMessagesAreInterpreted() returned non-zero return code.

Ran 4 tests.

FAILED (failures=4)
EOF
  )
  [ "$status" -eq 1 ]
  [ "$output" = "$expected" ]
}

@test "startSkipping turns the rest of the test's asserts into counted no-ops and ends with the test" {
  suite "$BATS_TEST_DIRNAME/suite_skip.sh"
  expected=$(
    cat <<'EOF'
testSkipsTheRest
isSkipping status: 0
testSkippingEndsWithTheTest
isSkipping status at start: 1

Ran 2 tests.

OK (skipped=3)
EOF
  )
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

@test "the line-number macro reports a usable line only in bash, and an unquoted message with a space breaks its arity" {
  local marker line
  marker=$(grep -n 'line-marker' "$BATS_TEST_DIRNAME/suite_lineno.sh")
  line=${marker%%:*}
  suite "$BATS_TEST_DIRNAME/suite_lineno.sh"
  [ "$status" -eq 1 ]
  [[ $output == *"shunit2:ERROR assertEquals() requires two or three arguments; 4 given"* ]]
  case $(lab_shell_family) in
    bash)
      [[ $output == *$'\nASSERT:['"$line"'] two words expected:<x> but was:<y>'* ]]
      ;;
    dash)
      # dash's LINENO inside eval is a negative offset, not the line in the file.
      grep -Eq '^ASSERT:\[-[0-9]+\] two words expected:<x> but was:<y>$' <<<"$output"
      ;;
    zsh | zsh-sh)
      grep -Eq '^ASSERT:\[1\] two words expected:<x> but was:<y>$' <<<"$output"
      ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) [[ $output == *"two words expected:<x> but was:<y>"* ]] ;;
        *) grep -Eq '^ASSERT:\[1\] two words expected:<x> but was:<y>$' <<<"$output" ;;
      esac
      ;;
    mksh | yash | posh | busybox)
      [[ $output == *"two words expected:<x> but was:<y>"* ]]
      ;;
    *) false ;;
  esac
}

# --- cost -----------------------------------------------------------------------------------

@test "every passing assertEquals costs two expr executions and a string assertTrue costs three" {
  local shim="$BATS_TEST_TMPDIR/shim" real n
  real=$(command -v expr)
  mkdir -p "$shim"
  cat >"$shim/expr" <<'EOF'
#!/bin/sh
echo x >>"$LAB_COUNT"
exec "$REAL_EXPR" "$@"
EOF
  chmod +x "$shim/expr"
  export LAB_COUNT="$BATS_TEST_TMPDIR/expr-calls" REAL_EXPR="$real"

  gen_suite count0.sh <<'EOF'
testA() { :; }
EOF
  : >"$LAB_COUNT"
  PATH="$shim:$PATH" suite "$SUITE"
  [ "$status" -eq 0 ]
  n=$(wc -l <"$LAB_COUNT")
  [ $((n)) -eq 2 ] # one for suite_addTest, one for the passed-test counter

  gen_suite count10.sh <<'EOF'
testA() {
  assertEquals 1 1
  assertEquals 2 2
  assertEquals 3 3
  assertEquals 4 4
  assertEquals 5 5
  assertEquals 6 6
  assertEquals 7 7
  assertEquals 8 8
  assertEquals 9 9
  assertEquals 10 10
}
EOF
  : >"$LAB_COUNT"
  PATH="$shim:$PATH" suite "$SUITE"
  [ "$status" -eq 0 ]
  n=$(wc -l <"$LAB_COUNT")
  [ $((n)) -eq 22 ] # 2 + 10 * 2

  gen_suite count1t.sh <<'EOF'
testA() { assertTrue '[ 1 -eq 1 ]'; }
EOF
  : >"$LAB_COUNT"
  PATH="$shim:$PATH" suite "$SUITE"
  [ "$status" -eq 0 ]
  n=$(wc -l <"$LAB_COUNT")
  [ $((n)) -eq 5 ] # 2 + 2 for the pass + 1 for the numeric-looking check
}

@test "mocking grep as a function breaks assertContains, because shUnit2 resolves grep like the code under test" {
  gen_suite grepmock.sh <<'EOF'
testWithMockedGrep() {
  grep() { cat >/dev/null; return 1; }
  assertContains 'hello world' hello
  unset -f grep
}
testWithRealGrep() {
  assertContains 'hello world' hello
}
EOF
  suite "$SUITE"
  [ "$status" -eq 1 ]
  [[ $output == *$'testWithMockedGrep\nASSERT:Not found:<hello>\ntestWithRealGrep\n'* ]]
  [[ $output == *$'Ran 2 tests.\n\nFAILED (failures=1)' ]]
}

# --- failure paths and exit status ----------------------------------------------------------

@test "a failing setUp is fatal: the run stops with status 2 and the message names setup()" {
  gen_suite setupfail.sh <<'EOF'
setUp() { return 1; }
testA() { echo ran-A; }
testB() { echo ran-B; }
EOF
  suite "$SUITE"
  expected=$(
    cat <<'EOF'
shunit2:FATAL setup() returned non-zero return code.
ASSERT:unknown failure encountered running a test

Ran 2 tests.

FAILED (failures=1)
EOF
  )
  [ "$status" -eq 2 ]
  [ "$output" = "$expected" ]
}

@test "a failing oneTimeSetUp is fatal before any test is counted" {
  gen_suite onetimefail.sh <<'EOF'
oneTimeSetUp() { return 1; }
testA() { echo ran-A; }
EOF
  suite "$SUITE"
  expected=$(
    cat <<'EOF'
shunit2:FATAL oneTimeSetUp() returned non-zero return code.
ASSERT:unknown failure encountered running a test

Ran 0 tests.

FAILED (failures=1)
EOF
  )
  [ "$status" -eq 2 ]
  [ "$output" = "$expected" ]
}

@test "exit inside a test, even exit 0, is reported as an unknown failure with status 2" {
  gen_suite exittest.sh <<'EOF'
testA() { assertEquals 1 1; exit 0; }
testB() { echo ran-B; }
EOF
  suite "$SUITE"
  expected=$(
    cat <<'EOF'
testA
ASSERT:unknown failure encountered running a test

Ran 2 tests.

FAILED (failures=1)
EOF
  )
  [ "$status" -eq 2 ]
  [ "$output" = "$expected" ]
}

@test "set -e: a passing assertEquals is fine, a passing string assertTrue kills the run" {
  gen_suite sete_eq.sh <<'EOF'
set -e
testA() { assertEquals 1 1; echo reached-after-assert; }
EOF
  suite "$SUITE"
  [ "$status" -eq 0 ]
  [ "$output" = $'testA\nreached-after-assert\n\nRan 1 test.\n\nOK' ]

  gen_suite sete_cond.sh <<'EOF'
set -e
testA() { assertTrue '[ 1 -eq 1 ]'; echo reached-after-assert; }
EOF
  suite "$SUITE"
  case $(lab_shell_family) in
    bash | dash | ksh93 | mksh | yash | posh | busybox)
      [ "$status" -eq 2 ]
      [ "$output" = $'testA\nASSERT:unknown failure encountered running a test\n\nRan 1 test.\n\nFAILED (failures=1)' ]
      ;;
    zsh | zsh-sh)
      # zsh leaves at the failed command substitution without running shUnit2's exit trap: no report at all.
      [ "$status" -eq 1 ]
      [ "$output" = testA ]
      ;;
    *) false ;;
  esac
}

@test "SIGTERM to the suite runs the cleanup handler and exits 143, except in zsh" {
  gen_suite termtest.sh <<'EOF'
tearDown() { echo 'hook: tearDown'; }
testA() { kill -TERM $$; echo not-reached; }
EOF
  suite "$SUITE"
  [[ $output == *$'testA\nshunit2:WARN trapped and now handling the (TERM) signal\nhook: tearDown'* ]]
  [[ $output != *not-reached* ]]
  case $(lab_shell_family) in
    bash | dash | ksh93 | mksh | yash | posh | busybox)
      [ "$status" -eq 143 ]
      ;;
    zsh | zsh-sh)
      # zsh's exit inside the TERM handler still reaches the EXIT trap, which reports an unknown failure.
      [ "$status" -eq 2 ]
      [[ $output == *"ASSERT:unknown failure encountered running a test"* ]]
      ;;
    *) false ;;
  esac
}

@test "sourcing shUnit2 when SHUNIT_VERSION is already set exits 0 silently and runs nothing" {
  cat >"$BATS_TEST_TMPDIR/twice.sh" <<'EOF'
#!/bin/sh
echo before
SHUNIT_VERSION=preloaded
testA() { echo SHOULD-NOT-RUN; }
. "$SHUNIT2_LIB"
echo after
EOF
  chmod +x "$BATS_TEST_TMPDIR/twice.sh"
  suite "$BATS_TEST_TMPDIR/twice.sh"
  [ "$status" -eq 0 ]
  [ "$output" = before ]
}

# --- temporary directory, traps, color ------------------------------------------------------

@test "shUnit2 takes its temporary directory from the mktemp on PATH and removes it on exit" {
  gen_suite tmpdir.sh <<'EOF'
oneTimeSetUp() { echo "tmpdir: $SHUNIT_TMPDIR"; }
testA() { assertTrue "[ -d '$SHUNIT_TMPDIR' ]"; }
EOF
  suite "$SUITE"
  [ "$status" -eq 0 ]
  [[ $output == "tmpdir: $LAB_TMP/shunit."*"/tmp"$'\n'* ]]
  count_shunit_dirs
  [ "$SHUNIT_DIRS" -eq 0 ]
}

@test "without a working mktemp, shUnit2 builds a directory under TMPDIR itself" {
  mkdir -p "$BATS_TEST_TMPDIR/failbin" "$BATS_TEST_TMPDIR/fallback"
  cat >"$BATS_TEST_TMPDIR/failbin/mktemp" <<'EOF'
#!/bin/sh
exit 1
EOF
  chmod +x "$BATS_TEST_TMPDIR/failbin/mktemp"
  gen_suite fallback.sh <<'EOF'
oneTimeSetUp() { echo "tmpdir: $SHUNIT_TMPDIR"; }
testA() { assertTrue "[ -d '$SHUNIT_TMPDIR' ]"; }
EOF
  PATH="$BATS_TEST_TMPDIR/failbin:$PATH" TMPDIR="$BATS_TEST_TMPDIR/fallback" suite "$SUITE"
  [ "$status" -eq 0 ]
  [[ $output == "tmpdir: $BATS_TEST_TMPDIR/fallback/shunit."*"/tmp"$'\n'* ]]
  count_shunit_dirs
  [ "$SHUNIT_DIRS" -eq 0 ]
  leftover=$(ls -A "$BATS_TEST_TMPDIR/fallback")
  [ -z "$leftover" ]
}

@test "a test that sets its own EXIT trap replaces shUnit2's cleanup and leaks the directory, except in zsh" {
  gen_suite traptest.sh <<'EOF'
testA() { trap 'echo my-trap' 0; assertEquals 1 1; }
EOF
  suite "$SUITE"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | ksh93 | mksh | yash | posh | busybox)
      [ "$output" = $'testA\n\nRan 1 test.\n\nOK\nmy-trap' ]
      count_shunit_dirs
      [ "$SHUNIT_DIRS" -eq 1 ]
      ;;
    zsh | zsh-sh)
      # In zsh an EXIT trap set inside a function runs when that function returns, and the outer trap survives.
      [ "$output" = $'testA\nmy-trap\n\nRan 1 test.\n\nOK' ]
      count_shunit_dirs
      [ "$SHUNIT_DIRS" -eq 0 ]
      ;;
    *) false ;;
  esac
}

@test "SHUNIT_COLOR: always and auto color a stream that is not a terminal, none does not, never is fatal" {
  local esc
  esc=$(printf '\033')
  gen_suite color.sh <<'EOF'
testA() { assertEquals 1 2; }
EOF
  SHUNIT_COLOR=always suite "$SUITE"
  [ "$status" -eq 1 ]
  [[ $output == *"${esc}[1;31mASSERT:${esc}[0mexpected:<1> but was:<2>"* ]]

  unset SHUNIT_COLOR
  TERM=xterm-256color suite "$SUITE"
  [ "$status" -eq 1 ]
  [[ $output == *"${esc}[1;31mASSERT:${esc}[0m"* ]]

  SHUNIT_COLOR=none suite "$SUITE"
  [ "$status" -eq 1 ]
  [[ $output != *"$esc"* ]]
  [[ $output == *$'\nASSERT:expected:<1> but was:<2>\n'* ]]

  SHUNIT_COLOR=never suite "$SUITE"
  [ "$status" -eq 2 ]
  [[ $output == *"shunit2:FATAL unrecognized color option 'never'"* ]]
}
