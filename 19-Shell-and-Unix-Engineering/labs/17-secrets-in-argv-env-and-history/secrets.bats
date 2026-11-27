#!/usr/bin/env bats
# Tests for secrets passing via argv, environment, stdin, and heredocs.

setup() {
  load ../lib/lab
  DEMO="$BATS_TEST_DIRNAME/secrets_demo.sh"
  PROBE="$BATS_TEST_TMPDIR/probe_channel"
  "${CC:-cc}" -O2 "$BATS_TEST_DIRNAME/probe_channel.c" -o "$PROBE"
}

@test "argv exposes secret in command-line arguments" {
  run_lab "$DEMO" argv "$PROBE" "top-secret-token"
  [ "$status" -eq 0 ]
  [ "$output" = "found in argv" ]
}

@test "stdin keeps secret out of argv and delivers payload" {
  run_lab "$DEMO" stdin "$PROBE" "top-secret-token"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 2 ]
  [ "${lines[0]}" = "not in argv" ]
  [ "${lines[1]}" = "stdin: top-secret-token" ]
}

@test "exported environment variable is inherited by child process" {
  run_lab "$DEMO" env_leak "$PROBE" "top-secret-token"
  [ "$status" -eq 0 ]
  [ "$output" = "env: top-secret-token" ]
}

@test "env -i scrubs exported environment variable from child process" {
  run_lab "$DEMO" env_scrub "$PROBE" "top-secret-token"
  [ "$status" -eq 0 ]
  [ "$output" = "env: unset" ]
}

@test "here-document backing store matches shell implementation" {
  local family version major minor
  run_lab "$DEMO" heredoc_type "$PROBE"
  [ "$status" -eq 0 ]
  family=$(lab_shell_family)
  case $family in
    bash)
      # Bash 5.1 NEWS 1.c: pipes for documents smaller than the pipe buffer, temporary files before.
      version=$(lab_shell_version)
      major=${version%%.*}
      minor=${version#*.}
      minor=${minor%%.*}
      if [ "$major" -gt 5 ] || { [ "$major" -eq 5 ] && [ "$minor" -ge 1 ]; }; then
        [ "$output" = "PIPE_OR_FIFO" ]
      else
        [ "$output" = "REGULAR_FILE size=7" ]
      fi
      ;;
    dash | yash | busybox)
      [ "$output" = "PIPE_OR_FIFO" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh | posh)
      [ "$output" = "REGULAR_FILE size=7" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "set -o history is accepted only by bash" {
  local family
  run_lab_c 'if (set -o history) >/dev/null 2>&1; then echo SUPPORTED; else echo UNSUPPORTED; fi'
  [ "$status" -eq 0 ]
  family=$(lab_shell_family)
  case $family in
    bash)
      [ "$output" = "SUPPORTED" ]
      ;;
    zsh | zsh-sh | dash | ksh93 | busybox | mksh | posh | yash)
      [ "$output" = "UNSUPPORTED" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "bash ignorespace hides only the space-led line; tab, plain, and block lines reach the history file" {
  local family hist script
  family=$(lab_shell_family)
  [ "$family" = bash ] || skip "HISTCONTROL is Bash-only"
  hist="$BATS_TEST_TMPDIR/test_hist"
  script="$BATS_TEST_TMPDIR/session.in"
  # The same lines as the note's predict-the-output question, fed to an interactive shell on stdin.
  printf ' echo hidden_payload\n\techo tab_payload\necho visible_payload\n{\n  echo block_payload\n}\nhistory -a\nexit\n' >"$script"
  export HISTFILE="$hist" HISTCONTROL=ignorespace
  lab_shell_words
  run "${LAB_SHELL_WORDS[@]}" --norc --noprofile -i <"$script"
  [ "$status" -eq 0 ]
  [ -f "$hist" ]
  run grep -c -F hidden_payload "$hist"
  [ "$output" = "0" ]
  grep -q -F 'echo tab_payload' "$hist"
  grep -q -F 'echo visible_payload' "$hist"
  grep -q -F 'echo block_payload' "$hist"
}
