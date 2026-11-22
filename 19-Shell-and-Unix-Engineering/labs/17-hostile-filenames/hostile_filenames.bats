#!/usr/bin/env bats
# Tests for hostile filename mechanics, traps, and defenses.
# shellcheck disable=SC2016 # code passed to run_lab_c is evaluated by the shell under test

setup() {
  load ../lib/lab
  SHOWARGS="$BATS_TEST_DIRNAME/../common/showargs.sh"
  INVENTORY="$BATS_TEST_DIRNAME/inventory.sh"
}

@test "leading hyphen causes option injection in cat unless protected by ./ or --" {
  mkdir -p "$BATS_TEST_TMPDIR/hyphen_test"
  printf 'hello from payload\n' >"$BATS_TEST_TMPDIR/hyphen_test/-n"

  # Unprefixed relative filename: cat interprets -n as the line-numbering flag reading stdin
  run_lab_c 'cd "$1" && f="-n" && cat "$f" </dev/null' sh "$BATS_TEST_TMPDIR/hyphen_test"
  # When cat sees -n, it reads stdin (empty /dev/null), so output is empty
  [ "$status" -eq 0 ]
  [ "$output" = "" ]

  # Protected by --: cat treats -n as operand
  run_lab_c 'cd "$1" && f="-n" && cat -- "$f"' sh "$BATS_TEST_TMPDIR/hyphen_test"
  [ "$status" -eq 0 ]
  [ "$output" = "hello from payload" ]

  # Protected by ./ prefix: filename starts with dot, never option flag
  run_lab_c 'cd "$1" && f="./-n" && cat "$f"' sh "$BATS_TEST_TMPDIR/hyphen_test"
  [ "$status" -eq 0 ]
  [ "$output" = "hello from payload" ]

  # Protected by redirection: no option parsing is possible
  run_lab_c 'cd "$1" && f="-n" && cat < "$f"' sh "$BATS_TEST_TMPDIR/hyphen_test"
  [ "$status" -eq 0 ]
  [ "$output" = "hello from payload" ]
}

@test "glob characters in unquoted variable undergo pathname expansion" {
  mkdir -p "$BATS_TEST_TMPDIR/glob_test"
  touch "$BATS_TEST_TMPDIR/glob_test/file1" "$BATS_TEST_TMPDIR/glob_test/file2"

  # When unquoted, variable containing wildcard expands to matched files
  # shellcheck disable=SC2086,SC2248 # testing unquoted expansion behavior
  run_lab_c 'cd "$1" && f="*" && "$2" $f' sh "$BATS_TEST_TMPDIR/glob_test" "$SHOWARGS"
  [ "$status" -eq 0 ]
  [ "$output" = "2 args: <file1> <file2>" ]

  # Quoted variable preserves literal wildcard character
  run_lab_c 'cd "$1" && f="*" && "$2" "$f"' sh "$BATS_TEST_TMPDIR/glob_test" "$SHOWARGS"
  [ "$status" -eq 0 ]
  [ "$output" = "1 args: <*>" ]
}

@test "whitespace in unquoted filename causes field splitting except in native zsh" {
  # shellcheck disable=SC2086,SC2248 # testing unquoted expansion behavior
  run_lab_c 'f="hello world.txt"; "$1" $f' sh "$SHOWARGS"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh)
      [ "$output" = "1 args: <hello world.txt>" ]
      ;;
    bash | dash | ksh93 | mksh | yash | busybox | posh | zsh-sh)
      [ "$output" = "2 args: <hello> <world.txt>" ]
      ;;
    *)
      false
      ;;
  esac

  # Quoting protects whitespace across every shell family
  run_lab_c 'f="hello world.txt"; "$1" "$f"' sh "$SHOWARGS"
  [ "$status" -eq 0 ]
  [ "$output" = "1 args: <hello world.txt>" ]
}

@test "embedded newline splits line-oriented read loop" {
  mkdir -p "$BATS_TEST_TMPDIR/nl_test"
  nl_name=$(printf 'line1\nline2')
  touch "$BATS_TEST_TMPDIR/nl_test/$nl_name"

  # Line-oriented pipeline splits single filename into two iterations
  run_lab_c '
    count=0
    for f in "$1"/*; do
      printf "%s\n" "$f"
    done | while read -r line; do
      count=$((count + 1))
      printf "%d:%s\n" "$count" "$line"
    done
  ' sh "$BATS_TEST_TMPDIR/nl_test"
  [ "$status" -eq 0 ]
  # Verifying that two lines were generated from one filename
  echo "$output" | grep -q "1:.*line1"
  echo "$output" | grep -q "2:line2"
}

@test "command substitution strips trailing newlines without sentinel" {
  # Command substitution strips trailing newlines per POSIX XCU 2.6.3
  run_lab_c '
    val=$(printf "data\n\n\n")
    printf "%d" "${#val}"
  '
  [ "$status" -eq 0 ]
  [ "$output" = "4" ]

  # Sentinel idiom preserves every trailing newline
  run_lab_c '
    raw=$(printf "data\n\n\nx")
    val=${raw%x}
    printf "%d" "${#val}"
  '
  [ "$status" -eq 0 ]
  [ "$output" = "7" ]
}

@test "read strips leading and trailing whitespace unless IFS is cleared" {
  # Without IFS=, read strips leading/trailing whitespace
  run_lab_c '
    printf "  padded filename  \n" | while read -r f; do
      printf "<%s>\n" "$f"
    done
  '
  [ "$status" -eq 0 ]
  [ "$output" = "<padded filename>" ]

  # With IFS=, read preserves exact whitespace
  run_lab_c '
    printf "  padded filename  \n" | while IFS= read -r f; do
      printf "<%s>\n" "$f"
    done
  '
  [ "$status" -eq 0 ]
  [ "$output" = "<  padded filename  >" ]
}

@test "inventory script identifies hostile filenames and summarizes counts" {
  testdir="$BATS_TEST_TMPDIR/inv_dir"
  mkdir -p "$testdir"
  touch "$testdir/-danger"
  touch "$testdir/spaced file.txt"
  touch "$testdir/clean.txt"

  run_lab "$INVENTORY" "$testdir"
  [ "$status" -eq 0 ]
  echo "$output" | grep -q "HOSTILE: -danger | flags: \[HYPHEN\]"
  echo "$output" | grep -q "HOSTILE: spaced file.txt | flags: \[SPACE\]"
  echo "$output" | grep -q "SUMMARY: total=3 hostile=2 clean=1"
}

@test "inventory script output matches the worked example exactly" {
  testdir="$BATS_TEST_TMPDIR/example_dir"
  mkdir -p "$testdir"
  touch "$testdir/-rf" "$testdir/glob*file[1].txt" "$testdir/space file.txt" "$testdir/clean.txt"

  LC_ALL=C run_lab "$INVENTORY" "$testdir"
  [ "$status" -eq 0 ]
  expected='HOSTILE: -rf | flags: [HYPHEN] | bytes: 0
HOSTILE: glob*file[1].txt | flags: [GLOB] | bytes: 0
HOSTILE: space file.txt | flags: [SPACE] | bytes: 0
SUMMARY: total=4 hostile=3 clean=1'
  [ "$output" = "$expected" ]
}

@test "xargs splits a name with a space unless NUL-delimited" {
  mkdir -p "$BATS_TEST_TMPDIR/xargs_test"
  touch "$BATS_TEST_TMPDIR/xargs_test/a b"

  # Plain xargs splits on the blank: one file becomes two operands
  run_lab_c 'cd "$1" && find . -type f | xargs "$2"' sh "$BATS_TEST_TMPDIR/xargs_test" "$SHOWARGS"
  [ "$status" -eq 0 ]
  [ "$output" = "2 args: <./a> <b>" ]

  # NUL-delimited input keeps the name as one operand
  run_lab_c 'cd "$1" && find . -type f -print0 | xargs -0 "$2"' sh "$BATS_TEST_TMPDIR/xargs_test" "$SHOWARGS"
  [ "$status" -eq 0 ]
  [ "$output" = "1 args: <./a b>" ]
}
