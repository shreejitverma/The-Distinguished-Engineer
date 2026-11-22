#!/usr/bin/env bats
# Bats lab matrix for ShellCheck Warning Families: the runtime behavior each warning family guards against.
# ShellCheck itself is not run here (the matrix images do not carry it); the note shows a hand-run capture.

setup() {
  load ../lib/lab
  export TMPDIR="$BATS_TEST_TMPDIR"
  PROBE="$BATS_TEST_DIRNAME/families-probe.sh"
}

# kv KEY: set KV to the value of the first KEY=value line of the probe output; fail if there is none.
kv() {
  local line
  KV=
  for line in "${lines[@]}"; do
    case $line in
      "$1="*)
        KV=${line#*=}
        return 0
        ;;
      *) ;;
    esac
  done
  return 1
}

# expect KEY VALUE: fail unless the probe printed KEY=VALUE.
expect() {
  kv "$1" && [ "$KV" = "$2" ]
}

# expect_positive KEY: fail unless the probe printed KEY=N with N greater than zero.
expect_positive() {
  kv "$1" && [ "$KV" -gt 0 ]
}

@test "SC2086 SC2046: unquoted expansions split and glob (native zsh does neither for parameters), quoting and the exceptions stop it" {
  run_lab "$PROBE" quoting
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh)
      expect split 1
      expect glob 1
      ;;
    bash | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      expect split 2
      expect glob 3
      ;;
    *) false ;;
  esac
  expect quoted-split 1
  expect quoted-glob 1
  expect unquoted-empty 0
  expect quoted-empty 1
  expect alt-value-empty 0
  expect noglob 1
  expect ifs-empty 1
  expect cmdsubst-split 2
  expect cmdsubst-quoted 1
}

@test "SC2155 SC2312: export and a surrounding command discard a command substitution status, a plain assignment keeps it" {
  run_lab "$PROBE" masking
  [ "$status" -eq 0 ]
  expect export-assign 0
  expect plain-assign 1
  expect last-false 1
  expect first-false 0
  expect errexit-export reached
  expect errexit-plain stopped
  expect useless-echo 0
}

@test "SC2181: a test of the status variable after another command reads that command's status, and errexit skips the handler" {
  run_lab "$PROBE" status
  [ "$status" -eq 0 ]
  expect indirect missed
  expect direct detected
  expect errexit-indirect handler-skipped
  expect errexit-direct handler-ran
}

@test "SC2164: after a failed cd the script keeps writing in the wrong directory unless the cd is checked" {
  run_lab "$PROBE" cd
  [ "$status" -eq 0 ]
  expect_positive cd-status
  expect unchecked wrote-in-wrong-directory
  expect checked safe
}

@test "SC2015: the AND-OR chain A then B else C runs C when A is true and B fails, an if does not" {
  run_lab "$PROBE" andor
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = and-or-ran-c ]
  [ "${lines[1]}" = if-done ]
  [ "${#lines[@]}" -eq 2 ]
}

@test "SC2162: read without -r eats backslashes and joins lines, IFS= keeps the padding" {
  run_lab "$PROBE" read
  [ "$status" -eq 0 ]
  expect raw 'a\b'
  expect cooked ab
  expect joined xy
  expect joined-raw "x\\"
  expect trimmed '<pad>'
  expect kept '<  pad  >'
}

@test "SC2064: a double-quoted trap string expands when the trap is set, a single-quoted one when it fires" {
  run_lab "$PROBE" trap
  [ "$status" -eq 0 ]
  expect double-quoted early
  expect single-quoted late
}

@test "SC2154: a misspelled variable expands to nothing, and set -u turns it into a failure" {
  run_lab "$PROBE" unset
  [ "$status" -eq 0 ]
  expect plain '<hello >'
  expect_positive set-u-status
  expect set-u-output '<>'
  expect target world
}

@test "SC2128 SC3030: array syntax is a syntax error, the first element, or all elements, depending on the shell" {
  run_lab "$PROBE" arrays
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    dash | posh | busybox) expect array rejected ;;
    bash | zsh-sh | ksh93 | mksh) expect array '<foo>' ;;
    zsh | yash) expect array '<foo><bar>' ;;
    *) false ;;
  esac
}

@test "SC2035: a glob as the first operand lets a file named -u act as an option, ./ and -- stop it" {
  run_lab "$PROBE" dashglob
  [ "$status" -eq 0 ]
  expect unsafe y-only
  expect prefixed x-and-y
  expect ended x-and-y
}

@test "SC2013: a for loop over command substitution output reads words and expands globs, while read reads lines" {
  run_lab "$PROBE" lines
  [ "$status" -eq 0 ]
  expect while-lines 2
  case $(lab_shell_family) in
    zsh)
      expect for-words 3
      [[ $output != *expanded=* ]]
      ;;
    bash | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      expect for-words 5
      [[ $output == *expanded=aa.txt* ]]
      [[ $output == *expanded=bb.txt* ]]
      ;;
    *) false ;;
  esac
}

@test "the probe removes its scratch directories and rejects an unknown mode with status 2" {
  run_lab "$PROBE" quoting
  [ "$status" -eq 0 ]
  run find "$BATS_TEST_TMPDIR" -mindepth 1
  [ -z "$output" ]
  run_lab "$PROBE" no-such-mode
  [ "$status" -eq 2 ]
  [[ $output == *"usage:"* ]]
}

@test "SC2310 SC2311: errexit is off inside an if condition everywhere, and inside a command substitution in bash outside POSIX mode and in BusyBox ash" {
  expected=yes
  family=$(lab_shell_family)
  if [ "$family" = bash ]; then
    run_lab_c '[ -o posix ]'
    [ "$status" -eq 0 ] || expected=no
  fi
  # BusyBox 1.37.0 ash does not inherit errexit into a command substitution (measured in both matrix images)
  [ "$family" != busybox ] || expected=no
  run_lab "$PROBE" errexit
  [ "$status" -eq 0 ]
  expect condition-output f-continuedcondition-true
  case $family in
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      expect subst-inherits-errexit "$expected"
      ;;
    *) false ;;
  esac
}
