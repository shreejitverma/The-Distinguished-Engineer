#!/usr/bin/env bats
# Tests for golden.sh and inventory.sh: capture a command into a stdout/stderr/status triple,
# scrub the volatile fields, compare byte for byte against a committed golden set, update on purpose.
# The test bodies are bash (bats); every golden.sh and inventory.sh run goes through the shell under test.

setup() {
  load ../lib/lab
  GOLDEN="$BATS_TEST_DIRNAME/golden.sh"
  INV="$BATS_TEST_DIRNAME/inventory.sh"
  GOLD_DIR="$BATS_TEST_DIRNAME/golden"
  lab_shell_words
  # The command that capture runs: the shell under test, then the script.
  SUT=("${LAB_SHELL_WORDS[@]}" "$INV")
  unset CI
}

# make_run NAME: create $BATS_TEST_TMPDIR/NAME/d holding four files whose C-locale order is "10 9 B a".
make_run() {
  mkdir -p "$BATS_TEST_TMPDIR/$1/d"
  : >"$BATS_TEST_TMPDIR/$1/d/B"
  : >"$BATS_TEST_TMPDIR/$1/d/a"
  : >"$BATS_TEST_TMPDIR/$1/d/10"
  : >"$BATS_TEST_TMPDIR/$1/d/9"
}

# make_set DIR STDOUT STDERR STATUS: write a hand-made golden set.
make_set() {
  mkdir -p "$1"
  printf '%s' "$2" >"$1/stdout"
  printf '%s' "$3" >"$1/stderr"
  printf '%s' "$4" >"$1/status"
}

@test "capture records stdout, stderr and the exit status as three separate files" {
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/out" "${SUT[@]}" no-such-dir
  [ "$status" -eq 0 ]
  [ ! -s "$BATS_TEST_TMPDIR/out/stdout" ]
  [ "$(cat "$BATS_TEST_TMPDIR/out/stderr")" = "inventory.sh: cannot enter no-such-dir" ]
  [ "$(cat "$BATS_TEST_TMPDIR/out/status")" = 1 ]
  # The status file is "1" plus a newline, byte for byte.
  printf '1\n' | cmp -s - "$BATS_TEST_TMPDIR/out/status"
}

@test "capture succeeds even when the command fails: the status is data, not the exit code" {
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/out" "${SUT[@]}"
  [ "$status" -eq 0 ]
  [ "$(cat "$BATS_TEST_TMPDIR/out/status")" = 2 ]
  [ "$(cat "$BATS_TEST_TMPDIR/out/stderr")" = "usage: inventory.sh DIR" ]
}

@test "capture scrubs the environment: the script cannot see a variable the caller exported" {
  make_run r
  export GOLDEN_LEAK=from-the-caller
  # Direct run: the probe really does read the variable.
  run_lab "$INV" "$BATS_TEST_TMPDIR/r/d"
  [ "$status" -eq 0 ]
  [[ "$output" == *"leak=from-the-caller"* ]]
  # Through capture: env -i removed it, and LC_ALL and TZ are pinned.
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/out" "${SUT[@]}" "$BATS_TEST_TMPDIR/r/d"
  [ "$status" -eq 0 ]
  grep -q '^lc_all=C tz=UTC leak=unset$' "$BATS_TEST_TMPDIR/out/stdout"
}

@test "the raw output of two runs differs, the scrubbed output is identical" {
  make_run a
  make_run b
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/a/out" "${SUT[@]}" "$BATS_TEST_TMPDIR/a/d"
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/b/out" "${SUT[@]}" "$BATS_TEST_TMPDIR/b/d"
  [ "$status" -eq 0 ]
  run cmp -s "$BATS_TEST_TMPDIR/a/out/stdout" "$BATS_TEST_TMPDIR/b/out/stdout"
  [ "$status" -eq 1 ]
  run_lab "$GOLDEN" scrub "$BATS_TEST_TMPDIR/a/out" "$BATS_TEST_TMPDIR/a"
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" scrub "$BATS_TEST_TMPDIR/b/out" "$BATS_TEST_TMPDIR/b"
  [ "$status" -eq 0 ]
  cmp -s "$BATS_TEST_TMPDIR/a/out/stdout" "$BATS_TEST_TMPDIR/b/out/stdout"
}

@test "scrub rewrites only the named fields: a digit run that is not a pid survives" {
  make_set "$BATS_TEST_TMPDIR/s" $'pid=4242 count=4242 at 2026-11-21T10:20:30Z in /x/y.z/a\n/x/yzz/a\n' $'pid=7\n' $'0\n'
  run_lab "$GOLDEN" scrub "$BATS_TEST_TMPDIR/s" /x/y.z
  [ "$status" -eq 0 ]
  # "." in the scrubbed path is literal: /x/yzz/a must not match /x/y.z.
  [ "$(cat "$BATS_TEST_TMPDIR/s/stdout")" = $'pid=<PID> count=4242 at <TIMESTAMP> in <TMPDIR>/a\n/x/yzz/a' ]
  [ "$(cat "$BATS_TEST_TMPDIR/s/stderr")" = 'pid=<PID>' ]
  # The status file is never rewritten.
  [ "$(cat "$BATS_TEST_TMPDIR/s/status")" = 0 ]
}

@test "scrubbed capture of the success run matches the committed golden set" {
  make_run r
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/r/out" "${SUT[@]}" "$BATS_TEST_TMPDIR/r/d"
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" scrub "$BATS_TEST_TMPDIR/r/out" "$BATS_TEST_TMPDIR/r"
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" check "$GOLD_DIR/inventory" "$BATS_TEST_TMPDIR/r/out"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "the error cases are goldens too: a missing directory and a missing argument" {
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/e1" "${SUT[@]}" no-such-dir
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" scrub "$BATS_TEST_TMPDIR/e1"
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" check "$GOLD_DIR/missing-dir" "$BATS_TEST_TMPDIR/e1"
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/e2" "${SUT[@]}"
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" scrub "$BATS_TEST_TMPDIR/e2"
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" check "$GOLD_DIR/usage" "$BATS_TEST_TMPDIR/e2"
  [ "$status" -eq 0 ]
}

@test "the glob order in the golden is the C-locale byte order, digits then upper case then lower case" {
  grep -q '^order: 10 9 B a$' "$GOLD_DIR/inventory/stdout"
  make_run r
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/r/out" "${SUT[@]}" "$BATS_TEST_TMPDIR/r/d"
  [ "$status" -eq 0 ]
  grep -q '^order: 10 9 B a$' "$BATS_TEST_TMPDIR/r/out/stdout"
}

@test "command substitution cannot see a missing final newline, cmp can" {
  make_set "$BATS_TEST_TMPDIR/g" $'a\n' '' $'0\n'
  make_set "$BATS_TEST_TMPDIR/x" 'a' '' $'0\n'
  # The weak comparison: both sides lose their trailing newlines (POSIX XCU 2.6.3), so they match.
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '[ "$(cat "$1")" = "$(cat "$2")" ]' x "$BATS_TEST_TMPDIR/g/stdout" "$BATS_TEST_TMPDIR/x/stdout"
  [ "$status" -eq 0 ]
  run_lab "$GOLDEN" check "$BATS_TEST_TMPDIR/g" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 1 ]
  [ "${lines[0]}" = "golden: stdout differs" ]
}

@test "a carriage return is a difference" {
  make_set "$BATS_TEST_TMPDIR/g" $'a\n' '' $'0\n'
  make_set "$BATS_TEST_TMPDIR/x" $'a\r\n' '' $'0\n'
  run_lab "$GOLDEN" check "$BATS_TEST_TMPDIR/g" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 1 ]
  [ "${lines[0]}" = "golden: stdout differs" ]
}

@test "a mismatch exits 1 and prints a unified diff without the header lines" {
  make_set "$BATS_TEST_TMPDIR/g" $'old\n' '' $'0\n'
  make_set "$BATS_TEST_TMPDIR/x" $'new\n' '' $'0\n'
  run_lab "$GOLDEN" check "$BATS_TEST_TMPDIR/g" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 1 ]
  [ "${lines[0]}" = "golden: stdout differs" ]
  [[ "${lines[1]}" == "@@ "* ]]
  [ "${lines[2]}" = "-old" ]
  [ "${lines[3]}" = "+new" ]
  [ "${#lines[@]}" -eq 4 ]
}

@test "a change in the exit status alone is a failure" {
  make_set "$BATS_TEST_TMPDIR/g" $'a\n' '' $'0\n'
  make_set "$BATS_TEST_TMPDIR/x" $'a\n' '' $'1\n'
  run_lab "$GOLDEN" check "$BATS_TEST_TMPDIR/g" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 1 ]
  [ "${lines[0]}" = "golden: status differs" ]
  [ "${lines[2]}" = "-0" ]
  [ "${lines[3]}" = "+1" ]
}

@test "a missing golden set is an error, never a pass and never created implicitly" {
  make_set "$BATS_TEST_TMPDIR/x" $'a\n' '' $'0\n'
  run_lab "$GOLDEN" check "$BATS_TEST_TMPDIR/no-golden" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 2 ]
  [[ "$output" == "golden: no golden set at "* ]]
  [ ! -e "$BATS_TEST_TMPDIR/no-golden" ]
}

@test "a golden set with a missing file is an error, not a partial pass" {
  make_set "$BATS_TEST_TMPDIR/g" $'a\n' '' $'0\n'
  rm "$BATS_TEST_TMPDIR/g/stderr"
  make_set "$BATS_TEST_TMPDIR/x" $'a\n' '' $'0\n'
  run_lab "$GOLDEN" check "$BATS_TEST_TMPDIR/g" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 2 ]
  [[ "$output" == *"golden file missing"* ]]
}

@test "update replaces the golden set, leaves no temporary files, and the next check passes" {
  make_set "$BATS_TEST_TMPDIR/g" $'old\n' '' $'0\n'
  make_set "$BATS_TEST_TMPDIR/x" $'new\n' $'warn\n' $'3\n'
  run_lab "$GOLDEN" update "$BATS_TEST_TMPDIR/g" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 0 ]
  # find lists dot files too, so a leftover ".stdout.new" would show up here.
  names=$(find "$BATS_TEST_TMPDIR/g" -type f | sed "s,.*/,," | sort | tr '\n' ' ')
  [ "$names" = "status stderr stdout " ]
  run_lab "$GOLDEN" check "$BATS_TEST_TMPDIR/g" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 0 ]
}

@test "update creates a golden set that did not exist, only when asked" {
  make_set "$BATS_TEST_TMPDIR/x" $'new\n' '' $'0\n'
  run_lab "$GOLDEN" update "$BATS_TEST_TMPDIR/fresh" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 0 ]
  cmp -s "$BATS_TEST_TMPDIR/x/stdout" "$BATS_TEST_TMPDIR/fresh/stdout"
}

@test "update is refused when CI is set, and the golden set is untouched" {
  make_set "$BATS_TEST_TMPDIR/g" $'old\n' '' $'0\n'
  make_set "$BATS_TEST_TMPDIR/x" $'new\n' '' $'0\n'
  export CI=true
  run_lab "$GOLDEN" update "$BATS_TEST_TMPDIR/g" "$BATS_TEST_TMPDIR/x"
  [ "$status" -eq 2 ]
  [ "$output" = "golden: refusing to update golden files when CI is set" ]
  [ "$(cat "$BATS_TEST_TMPDIR/g/stdout")" = old ]
}

@test "unknown subcommands and missing arguments are usage errors" {
  run_lab "$GOLDEN" frobnicate
  [ "$status" -eq 2 ]
  run_lab "$GOLDEN" check onlyone
  [ "$status" -eq 2 ]
  run_lab "$GOLDEN" capture "$BATS_TEST_TMPDIR/out"
  [ "$status" -eq 2 ]
  [ ! -e "$BATS_TEST_TMPDIR/out" ]
}
