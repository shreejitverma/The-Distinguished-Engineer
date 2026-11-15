#!/usr/bin/env bats
# Tests for Dry Run and Apply: set -n syntax latch, redirection leakage,
# argument quoting serialization, simulation fallacy state divergence,
# two-phase plan-and-apply drift contracts, and safety latches.
# Code under test runs only through run_lab SCRIPT or run_lab_c CODE (labs/lib/lab.bash).

# shellcheck disable=SC2016 # expressions in run_lab_c expand in the shell under test

setup() {
  load ../lib/lab
  ENGINE="$BATS_TEST_DIRNAME/dry-run-engine.sh"
}

@test "set -n is an irreversible one-way syntax latch in every shell" {
  # One command per line: mksh, yash, and posh apply set -n only from the next
  # parsed line, so a one-line probe would print "invisible" there.
  probe="$BATS_TEST_TMPDIR/set-n-probe.sh"
  printf '%s\n' 'echo start' 'set -n' 'echo invisible' 'set +n' 'echo restored' >"$probe"
  run_lab "$probe"
  [ "$status" -eq 0 ]
  [ "$output" = "start" ]
}

@test "naive redirection truncates target file during dry run while safe write preserves state" {
  tmp_file="$BATS_TEST_TMPDIR/leak_test.txt"
  run_lab "$ENGINE" redirection_leak "$tmp_file"
  [ "$status" -eq 0 ]

  [[ "$output" == *"size_after_naive=0"* ]]
  [[ "$output" == *"content_after_safe=initial content"* ]]
}

@test "argument quoting serialization preserves spaces and escapes quotes without eval" {
  run_lab "$ENGINE" quote_serialize rm -f "file with spaces.txt" "it's dangerous" '$VAR' '*' '~root'
  [ "$status" -eq 0 ]
  expected="rm -f 'file with spaces.txt' 'it'\\''s dangerous' '\$VAR' '*' '~root'"
  [ "$output" = "$expected" ]
}

@test "simulation divergence fails on naive intermediate state while plan manifest succeeds" {
  workdir="$BATS_TEST_TMPDIR/sim_work"
  mkdir -p "$workdir"
  run_lab "$ENGINE" simulation_divergence "$workdir"
  [ "$status" -eq 0 ]

  [[ "$output" == *"naive_divergence_failed=1"* ]]
  [[ "$output" == *"two_phase_plan_records=2"* ]]
}

@test "two-phase sync dry run emits pending actions and exits 2 on drift" {
  src="$BATS_TEST_TMPDIR/sync_src"
  dst="$BATS_TEST_TMPDIR/sync_dst"
  mkdir -p "$src" "$dst"
  printf 'config_v1\n' >"$src/app.conf"
  printf 'doc\n' >"$src/README.md"
  printf 'stale\n' >"$dst/stale.log"

  run_lab "$ENGINE" sync -n "$src" "$dst"
  [ "$status" -eq 2 ]

  [[ "$output" == *"Plan: 3 actions pending"* ]]
  [[ "$output" == *"ADD	app.conf"* ]]
  [[ "$output" == *"DELETE	stale.log"* ]]
}

@test "two-phase sync apply executes mutations and subsequent dry run exits 0" {
  src="$BATS_TEST_TMPDIR/apply_src"
  dst="$BATS_TEST_TMPDIR/apply_dst"
  mkdir -p "$src" "$dst"
  printf 'config_v1\n' >"$src/app.conf"
  printf 'doc\n' >"$src/README.md"
  printf 'stale\n' >"$dst/stale.log"

  run_lab "$ENGINE" sync --apply "$src" "$dst"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Apply: 3 actions applied successfully"* ]]

  [ -f "$dst/app.conf" ]
  [ -f "$dst/README.md" ]
  [ ! -f "$dst/stale.log" ]

  run_lab "$ENGINE" sync -n "$src" "$dst"
  [ "$status" -eq 0 ]
  [[ "$output" == *"Plan: 0 actions pending"* ]]
}

@test "safety latch blocks mutations without explicit apply flag" {
  run_lab "$ENGINE" safety_latch
  [ "$status" -eq 2 ]
  [[ "$output" == *"Error: safety latch engaged"* ]]

  run_lab "$ENGINE" safety_latch --apply
  [ "$status" -eq 0 ]
  [[ "$output" == *"Safety latch released"* ]]
}
