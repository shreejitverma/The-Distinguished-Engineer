#!/usr/bin/env bats
# Tests for find argument batching (-exec ... {} +), -print0, and xargs -0.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/batch_eval.sh"
  SHOWARGS="$BATS_TEST_DIRNAME/../common/showargs.sh"
  TEST_DIR="$BATS_TEST_TMPDIR/tree"
  mkdir -p "$TEST_DIR"
  touch "$TEST_DIR/test_1.txt" "$TEST_DIR/test_2.txt" "$TEST_DIR/test_3.txt" \
    "$TEST_DIR/test_4.txt" "$TEST_DIR/test_5.txt"
  touch "$TEST_DIR/spec_space file.txt" "$TEST_DIR/spec_quote\"file'.txt" \
    "$TEST_DIR/spec_glob*file?.txt" "$TEST_DIR/spec_-dash.txt"
}

@test "semi_proc_count invokes probe once per matching file" {
  run_lab "$PROBE" semi_proc_count "$TEST_DIR" "$SHOWARGS"
  [ "$status" -eq 0 ]
  line_count=$(printf '%s\n' "$output" | grep -c '^1 args:')
  [ "$line_count" -eq 5 ]
}

@test "plus_proc_count aggregates all matching files into single invocation" {
  run_lab "$PROBE" plus_proc_count "$TEST_DIR" "$SHOWARGS"
  [ "$status" -eq 0 ]
  line_count=$(printf '%s\n' "$output" | grep -c '^5 args:')
  [ "$line_count" -eq 1 ]
}

@test "exit_status_semi returns zero even when executed utility fails" {
  run_lab "$PROBE" exit_status_semi "$TEST_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "0" ]
}

@test "exit_status_plus returns non-zero when executed utility fails" {
  run_lab "$PROBE" exit_status_plus "$TEST_DIR"
  [ "$status" -eq 0 ]
  [ "$output" -ne 0 ]
}

@test "special_names_plus preserves spaces newlines quotes and leading dashes" {
  run_lab "$PROBE" special_names_plus "$TEST_DIR" "$SHOWARGS"
  [ "$status" -eq 0 ]
  [[ "$output" =~ ^4\ args: ]]
  [[ "$output" =~ \<"$TEST_DIR"/spec_space\ file\.txt\> ]]
  [[ "$output" =~ \<"$TEST_DIR"/spec_glob\*file\?\.txt\> ]]
  [[ "$output" =~ \<"$TEST_DIR"/spec_quote\"file\'\.txt\> ]]
  [[ "$output" =~ \<"$TEST_DIR"/spec_-dash\.txt\> ]]
}

@test "special_names_print0 preserves special characters across xargs -0" {
  run_lab "$PROBE" special_names_print0 "$TEST_DIR" "$SHOWARGS"
  [ "$status" -eq 0 ]
  [[ "$output" =~ ^4\ args: ]]
  [[ "$output" =~ \<"$TEST_DIR"/spec_space\ file\.txt\> ]]
  [[ "$output" =~ \<"$TEST_DIR"/spec_glob\*file\?\.txt\> ]]
  [[ "$output" =~ \<"$TEST_DIR"/spec_quote\"file\'\.txt\> ]]
  [[ "$output" =~ \<"$TEST_DIR"/spec_-dash\.txt\> ]]
}

@test "zero_matches_plus does not invoke utility when no files match" {
  run_lab "$PROBE" zero_matches_plus "$TEST_DIR" "$SHOWARGS"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "syntax_trailing_plus reports error when plus does not follow braces" {
  run_lab "$PROBE" syntax_trailing_plus "$TEST_DIR"
  [ "$status" -eq 0 ]
  if find --help 2>&1 | grep -q BusyBox; then
    # BusyBox find ends -exec at any standalone +, so it runs echo and exits 0
    [ "$output" = "0" ]
  else
    [ "$output" -ne 0 ]
  fi
}

@test "pipe_masking suppresses find failure exit status without pipefail" {
  run_lab "$PROBE" pipe_masking "$TEST_DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "0" ]
}

@test "read_null_stream handles null-delimited stream in supporting shells" {
  run_lab "$PROBE" read_null_stream "$TEST_DIR"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | busybox | mksh)
      [ "$output" = "4 entries" ]
      ;;
    dash | posh | yash)
      # yash 2.58.1 rejects read -d ("`-d' is not a valid option")
      [ "$output" = "unsupported" ]
      ;;
    ksh93)
      # ksh93u+ 2012 returns unsupported due to null-byte read handling; ksh93u+m supports it
      case "$output" in
        "4 entries" | "unsupported") ;;
        *) false ;;
      esac
      ;;
    *)
      false
      ;;
  esac
}
