#!/usr/bin/env bats
# Tests for find expression evaluation, operator precedence, -prune, and path safety.

# detect_find: set impl to gnu, busybox, or bsd; find is an external utility, so some expected
# values follow the find implementation rather than the shell under test.
detect_find() {
  impl=bsd
  run find --version
  if [ "$status" -eq 0 ]; then
    impl=gnu
  else
    run find --help
    case "$output" in
      *BusyBox*) impl=busybox ;;
      *) ;;
    esac
  fi
}

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/find_eval.sh"
  TEST_DIR="$BATS_TEST_TMPDIR/tree"
  mkdir -p "$TEST_DIR/skip" "$TEST_DIR/-v"
  touch "$TEST_DIR/a.txt" "$TEST_DIR/b.md" "$TEST_DIR/c.log"
  touch "$TEST_DIR/skip/nested.txt" "$TEST_DIR/-v/dash.txt"
  head -c 1025 /dev/zero >"$TEST_DIR/sized.bin"
}

@test "unparenthesized alternation binds -print only to right operand" {
  run_lab "$PROBE" precedence_unparenthesized "$TEST_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "$TEST_DIR/b.md" ]
}

@test "parenthesized alternation prints matching files from both branches" {
  run_lab "$PROBE" precedence_parenthesized "$TEST_DIR"
  [ "$status" -eq 0 ]
  expected=$(printf '%s\n' \
    "$TEST_DIR/-v/dash.txt" \
    "$TEST_DIR/a.txt" \
    "$TEST_DIR/b.md" \
    "$TEST_DIR/skip/nested.txt")
  [ "$output" = "$expected" ]
}

@test "default print wraps entire expression when no action is specified" {
  run_lab "$PROBE" default_print "$TEST_DIR"
  [ "$status" -eq 0 ]
  expected=$(printf '%s\n' \
    "$TEST_DIR/-v/dash.txt" \
    "$TEST_DIR/a.txt" \
    "$TEST_DIR/b.md" \
    "$TEST_DIR/skip/nested.txt")
  [ "$output" = "$expected" ]
}

@test "bare -prune without action prints only the pruned directory" {
  run_lab "$PROBE" prune_bare "$TEST_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "$TEST_DIR/skip" ]
}

@test "prune with alternation and print excludes pruned subtree" {
  run_lab "$PROBE" prune_correct "$TEST_DIR"
  [ "$status" -eq 0 ]
  expected=$(printf '%s\n' \
    "$TEST_DIR/-v/dash.txt" \
    "$TEST_DIR/a.txt")
  [ "$output" = "$expected" ]
}

@test "depth traversal disables prune and visits nested entries" {
  run_lab "$PROBE" prune_depth "$TEST_DIR"
  [ "$status" -eq 0 ]
  expected=$(printf '%s\n' \
    "$TEST_DIR/-v/dash.txt" \
    "$TEST_DIR/a.txt" \
    "$TEST_DIR/skip/nested.txt")
  [ "$output" = "$expected" ]
}

@test "short-circuit skips right operand when left operand is false" {
  trace="$BATS_TEST_TMPDIR/trace.log"
  touch "$trace"
  run_lab "$PROBE" short_circuit "$TEST_DIR" "$trace"
  [ "$status" -eq 0 ]
  # Only .txt files must have triggered the exec action
  run sort "$trace"
  expected=$(printf '%s\n' \
    "$TEST_DIR/-v/dash.txt" \
    "$TEST_DIR/a.txt" \
    "$TEST_DIR/skip/nested.txt")
  [ "$output" = "$expected" ]
}

@test "safe_path prefixes leading dash to avoid option misinterpretation" {
  (
    cd "$TEST_DIR"
    run_lab "$PROBE" safe_path "-v"
    [ "$status" -eq 0 ]
    [ "$output" = "./-v/dash.txt" ]
  )
}

@test "double dash ends leading options and still accepts a path operand" {
  run_lab "$PROBE" double_dash "$TEST_DIR"
  [ "$status" -eq 0 ]
  expected=$(printf '%s\n' \
    "$TEST_DIR/-v/dash.txt" \
    "$TEST_DIR/a.txt" \
    "$TEST_DIR/skip/nested.txt")
  [ "$output" = "$expected" ]
}

@test "double dash does not turn a hyphen-led operand into a path" {
  (
    cd "$TEST_DIR"
    run_lab "$PROBE" double_dash_hyphen "-v"
    [ "$status" -eq 0 ]
    [ "$output" = "rejected" ]
  )
}

@test "delete implies depth: GNU refuses -prune with -delete, BSD and BusyBox let it delete inside the skipped directory" {
  detect_find
  run_lab "$PROBE" delete_prune "$TEST_DIR"
  if [ "$impl" = gnu ]; then
    [ "$status" -eq 1 ]
    [[ "$output" == *"-delete action automatically turns on -depth, but -prune does nothing when -depth is in effect"* ]]
  else
    [ "$status" -eq 0 ]
    expected=$(printf '%s\n' \
      "$TEST_DIR" \
      "$TEST_DIR/-v" \
      "$TEST_DIR/b.md" \
      "$TEST_DIR/c.log" \
      "$TEST_DIR/sized.bin" \
      "$TEST_DIR/skip")
    [ "$output" = "$expected" ]
  fi
}

@test "explicit depth with prune and delete removes matches inside the skipped directory on every find" {
  run_lab "$PROBE" delete_prune_depth "$TEST_DIR"
  [ "$status" -eq 0 ]
  expected=$(printf '%s\n' \
    "$TEST_DIR" \
    "$TEST_DIR/-v" \
    "$TEST_DIR/b.md" \
    "$TEST_DIR/c.log" \
    "$TEST_DIR/sized.bin" \
    "$TEST_DIR/skip")
  [ "$output" = "$expected" ]
}

@test "size on a 1025-byte file: GNU rounds up to whole units, BSD and BusyBox compare scaled units exactly" {
  detect_find
  case "$impl" in
    gnu) expected=$(printf '%s\n' "1k no" "2k yes" "1025c yes" "3 yes" "-2k no" "+1k yes") ;;
    busybox) expected=$(printf '%s\n' "1k no" "2k no" "1025c yes" "3 no" "-2k yes" "+1k yes") ;;
    *) expected=$(printf '%s\n' "1k no" "2k no" "1025c yes" "3 yes" "-2k yes" "+1k yes") ;;
  esac
  run_lab "$PROBE" size_units "$TEST_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}
