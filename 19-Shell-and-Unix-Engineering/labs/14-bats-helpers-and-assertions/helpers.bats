#!/usr/bin/env bats
# Tests for the mechanics behind bats helpers and assertions: what `run` captures, how libraries
# are found and loaded, and how an assertion library (mini-assert, a small reimplementation of the
# bats-support/bats-assert mechanism) reads $status, $output and $lines and reports failure.
# Everything the code under test does goes through run_lab/run_lab_c, so LAB_SHELL swaps the shell
# under test; bats itself and the assertion library are bash.

setup() {
  load ../lib/lab
  bats_require_minimum_version 1.10.0
  export BATS_LIB_PATH="$BATS_TEST_DIRNAME/libs"
  bats_load_library mini-assert
  REPORT="$BATS_TEST_DIRNAME/report.sh"
}

# run_lab_flags RUN_FLAG... -- ARG...: like run_lab, with flags for `run` itself before the shell.
run_lab_flags() {
  local -a flags=()
  while [ "$1" != -- ]; do
    flags+=("$1")
    shift
  done
  shift
  lab_shell_words
  run "${flags[@]}" "${LAB_SHELL_WORDS[@]}" "$@"
}

# --- what run captures -------------------------------------------------------------------------

@test "run merges stderr into output and strips trailing newlines" {
  run_lab "$REPORT" 0
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'alpha\n\nbeta\n\n\nwarn: disk almost full')" ]
}

@test "run drops empty lines from lines by default" {
  run_lab "$REPORT" 0
  [ "${#lines[@]}" -eq 3 ]
  [ "${lines[0]}" = alpha ]
  [ "${lines[1]}" = beta ]
  [ "${lines[2]}" = "warn: disk almost full" ]
}

@test "run --keep-empty-lines keeps empty lines and the trailing newline" {
  run_lab_flags --keep-empty-lines -- "$REPORT" 0
  [ "${#lines[@]}" -eq 6 ]
  [ "${lines[0]}" = alpha ]
  [ -z "${lines[1]}" ]
  [ "${lines[2]}" = beta ]
  [ -z "${lines[3]}" ]
  [ -z "${lines[4]}" ]
  [ "${lines[5]}" = "warn: disk almost full" ]
  [ "${output: -1}" = $'\n' ]
}

@test "run --separate-stderr fills stderr and stderr_lines and leaves them out of output" {
  run_lab_flags --separate-stderr -- "$REPORT" 0
  [ "$output" = "$(printf 'alpha\n\nbeta')" ]
  [ "$stderr" = "warn: disk almost full" ]
  [ "${#stderr_lines[@]}" -eq 1 ]
  [ "${#lines[@]}" -eq 2 ]
}

@test "run succeeds whatever the status and records BATS_RUN_COMMAND" {
  run_lab "$REPORT" 7
  [ "$status" -eq 7 ]
  [ "$BATS_RUN_COMMAND" = "${LAB_SHELL:-sh} $REPORT 7" ]
}

@test "run -N and run ! check the status and return 1 on a mismatch" {
  run_lab_flags -7 -- "$REPORT" 7
  run_lab_flags ! -- "$REPORT" 7
  if run_lab_flags -3 -- "$REPORT" 7; then false; fi
  [ "$status" -eq 7 ]
  if run_lab_flags ! -- "$REPORT" 0; then false; fi
  [ "$status" -eq 0 ]
}

@test "a missing command reports status 127 inside the shell" {
  run_lab_c '{ no-such-command-xyz-123; } 2>/dev/null; echo "status=$?"'
  [ "$status" -eq 0 ]
  [ "$output" = "status=127" ]
}

@test "assert_failure without a status accepts any nonzero status, so pin the status" {
  run_lab "$REPORT" 2
  assert_failure
  assert_failure 2
  run assert_failure 1
  [ "$status" -eq 1 ]
  assert_line -p "-- command failed as expected, but status differs --"
  assert_line "expected : 1"
  assert_line "actual   : 2"
}

@test "a signal-killed child gives 128+N in most shells, 256+N in ksh93 and 384+N in yash" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '{ /bin/sh -c "kill -TERM \$\$"; } 2>/dev/null; echo $?'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | busybox | posh | mksh | dash) expected=143 ;;
    ksh93) expected=271 ;;
    yash) expected=399 ;;
    *) false ;;
  esac
  assert_output "$expected"
}

@test "set -e does not exit for a negated pipeline, which is why bats needs run !" {
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'set -e; ! true; echo reached'
  [ "$status" -eq 0 ]
  [ "$output" = reached ]
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'set -e; false; echo reached'
  [ "$status" -eq 1 ]
  [ -z "$output" ]
}

# --- run, subshells and pipes --------------------------------------------------------------------

set_flag() {
  # shellcheck disable=SC2209 # assigns the string "set"
  FLAG=set
}

@test "run executes a function in a subshell, so its assignments are lost" {
  unset FLAG
  run set_flag
  [ "$status" -eq 0 ]
  [ -z "${FLAG-}" ]
  set_flag
  [ "$FLAG" = set ]
}

@test "run CMD | filter captures nothing, while run bats_pipe keeps the output" {
  unset output
  # shellcheck disable=SC2312 # the pipeline status is irrelevant, the point is what run leaves behind
  run_lab_c 'echo foo' | cat >/dev/null
  [ -z "${output-}" ]
  lab_shell_words
  run bats_pipe "${LAB_SHELL_WORDS[@]}" -c 'echo foo; echo bar' \| grep bar
  [ "$status" -eq 0 ]
  [ "$output" = bar ]
}

@test "bats_pipe returns the last failing status, and -N picks one stage" {
  lab_shell_words
  run bats_pipe "${LAB_SHELL_WORDS[@]}" -c 'exit 3' \| cat
  [ "$status" -eq 3 ]
  run bats_pipe -1 "${LAB_SHELL_WORDS[@]}" -c 'exit 3' \| cat
  [ "$status" -eq 0 ]
  run bats_pipe -0 "${LAB_SHELL_WORDS[@]}" -c 'exit 3' \| cat
  [ "$status" -eq 3 ]
  run bats_pipe true
  [ "$status" -eq 1 ]
  assert_line -p "Usage error: No"
}

# --- load and bats_load_library ------------------------------------------------------------------

@test "load runs inside a function: a bare declare is local, a plain assignment is global" {
  unset PROBE_DECLARED PROBE_PLAIN
  load declare_probe
  [ -z "${PROBE_DECLARED+set}" ]
  [ "$PROBE_PLAIN" = plain ]
}

@test "bats_load_library takes the first match along BATS_LIB_PATH" {
  mkdir -p "$BATS_TEST_TMPDIR/a/shadow" "$BATS_TEST_TMPDIR/b/shadow"
  echo 'SHADOW=a' >"$BATS_TEST_TMPDIR/a/shadow/load.bash"
  echo 'SHADOW=b' >"$BATS_TEST_TMPDIR/b/shadow/load.bash"
  BATS_LIB_PATH="$BATS_TEST_TMPDIR/a:$BATS_TEST_TMPDIR/b" bats_load_library shadow
  [ "$SHADOW" = a ]
  BATS_LIB_PATH="$BATS_TEST_TMPDIR/b:$BATS_TEST_TMPDIR/a" bats_load_library shadow
  [ "$SHADOW" = b ]
}

@test "bats_load_library_safe returns 1 with a message instead of exiting" {
  run bats_load_library_safe no-such-library-xyz
  [ "$status" -eq 1 ]
  [ "$output" = "Could not find library 'no-such-library-xyz' relative to test file or in BATS_LIB_PATH" ]
}

# --- the assertion mechanism ---------------------------------------------------------------------

@test "the decorator keeps working when a test puts a fake cat first on PATH" {
  mkdir -p "$BATS_TEST_TMPDIR/bin"
  printf '#!/bin/sh\necho FAKE-CAT\n' >"$BATS_TEST_TMPDIR/bin/cat"
  chmod +x "$BATS_TEST_TMPDIR/bin/cat"
  PATH="$BATS_TEST_TMPDIR/bin:$PATH"
  run bash -c 'echo msg | cat -'
  [ "$output" = FAKE-CAT ]
  run assert_equal have want
  [ "$status" -eq 1 ]
  assert_line "expected : want"
  refute_output -p FAKE-CAT
}

@test "a literal mismatch prints a titled block with expected and actual" {
  run_lab_c 'echo have'
  assert_output have
  run assert_output want
  [ "$status" -eq 1 ]
  [ "${#lines[@]}" -eq 4 ]
  [ "${lines[0]}" = "-- output differs --" ]
  [ "${lines[1]}" = "expected : want" ]
  [ "${lines[2]}" = "actual   : have" ]
  [ "${lines[3]}" = "--" ]
}

@test "assert_output --partial and --regexp match substrings and patterns" {
  run_lab_c 'echo "ERROR: no such file"'
  assert_output -p "no such"
  assert_output -e '^ERROR: [a-z ]+$'
  run assert_output -p SUCCESS
  [ "$status" -eq 1 ]
  assert_line "-- output does not contain substring --"
  run_lab_c 'echo "ERROR: no such file"'
  run assert_output -e '^SUCCESS'
  [ "$status" -eq 1 ]
  assert_line "-- regular expression does not match output --"
}

@test "an invalid regular expression is an error, not a mismatch" {
  run_lab_c 'echo anything'
  run assert_output -e '('
  [ "$status" -eq 1 ]
  assert_line "-- ERROR: assert_output --"
  assert_line -p "Invalid extended regular expression"
}

@test "regexp anchors bind to the whole output, not to each line" {
  run_lab_c 'printf "one\ntwo\n"'
  assert_output -e '^one'
  assert_output -e 'two$'
  run assert_output -e '^two$'
  [ "$status" -eq 1 ]
  run_lab_c 'printf "one\ntwo\n"'
  assert_line two
  assert_line -e '^two$'
}

@test "assert_output with no argument means non-empty and refute_output with none means empty" {
  run_lab_c 'echo x'
  assert_output
  run refute_output
  [ "$status" -eq 1 ]
  assert_line "-- output non-empty, but expected no output --"
  run_lab_c 'true'
  refute_output
  run assert_output
  [ "$status" -eq 1 ]
  assert_line "expected non-empty output, but output was empty"
}

@test "assert_line matches exactly, as a substring, or at an index into the non-empty lines" {
  run_lab "$REPORT" 0
  assert_line beta
  assert_line -p "disk almost"
  assert_line -n 1 beta
  run assert_line -n 1 alpha
  [ "$status" -eq 1 ]
  assert_line "actual   : beta"
  run_lab "$REPORT" 0
  run assert_line -n 5 beta
  [ "$status" -eq 1 ]
  assert_line "actual   : "
  run_lab "$REPORT" 0
  run assert_line ""
  [ "$status" -eq 1 ]
}

@test "assert_equal takes the actual value first and the expected value second" {
  assert_equal same same
  run assert_equal have want
  [ "$status" -eq 1 ]
  assert_line "expected : want"
  assert_line "actual   : have"
}

@test "assert_success reports the status and the output of the failed command" {
  run_lab "$REPORT" 0
  assert_success
  run_lab "$REPORT" 2
  run assert_success
  [ "$status" -eq 1 ]
  assert_line "-- command failed --"
  assert_line "status : 2"
  assert_line -p "output (6 lines):"
  assert_line "  alpha"
}

@test "multi-line values are printed as indented blocks with a line count" {
  run_lab_c 'printf "a\nb\n"'
  run assert_output x
  [ "$status" -eq 1 ]
  assert_line "expected (1 lines):"
  assert_line "  x"
  assert_line "actual (2 lines):"
  assert_line "  a"
  assert_line "  b"
}

@test "assert takes a simple command, so a compound [[ needs bash -c" {
  assert [ -d "$BATS_TEST_TMPDIR" ]
  run assert [[ a == a ]]
  [ "$status" -eq 1 ]
  assert_line "-- assertion failed --"
  assert bash -c '[[ a == a ]]'
}

# --- the real runner: failure text, negation, skip ------------------------------------------------

@test "a nested bats run shows what a bare [ ] hides, what a negation skips, and what skip does" {
  cat >"$BATS_TEST_TMPDIR/nested.bats" <<'NESTED'
#!/usr/bin/env bats
bats_require_minimum_version 1.5.0

setup() {
  bats_load_library mini-assert
}

@test "neg-mid" {
  ! true
  true
}

@test "run-neg" {
  run ! true
}

@test "raw-bracket" {
  run printf have
  [ "$output" = want ]
}

@test "mini-assert" {
  run printf have
  assert_output want
}

@test "skipper" {
  skip "why"
  false
}
NESTED
  run bats --tap "$BATS_TEST_TMPDIR/nested.bats"
  [ "$status" -eq 1 ]
  assert_line "1..5"
  assert_line "ok 1 neg-mid"
  assert_line "not ok 2 run-neg"
  assert_line "not ok 3 raw-bracket"
  assert_line "not ok 4 mini-assert"
  assert_line "ok 5 skipper # skip why"
  # shellcheck disable=SC2016 # the backtick and quotes are literal text of the failure message
  assert_line -p '`[ "$output" = want ]'"'"' failed'
  assert_line "# expected : want"
  assert_line "# actual   : have"
  # the bare [ ] failure is reported without either value: "have" appears only in the mini-assert block
  count=$(printf '%s\n' "$output" | grep -c 'have')
  [ "$count" -eq 1 ]
}
