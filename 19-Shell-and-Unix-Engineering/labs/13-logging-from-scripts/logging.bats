#!/usr/bin/env bats
# Tests for Logging from Scripts: descriptor separation, a saved log descriptor, builtin versus
# forked timestamps, process substitution and wait, O_APPEND under concurrent writers, pipeline
# status through tee, logfmt and JSON escaping, systemd level prefixes, log injection
# (CWE-117, CWE-150), and BASH_XTRACEFD.
# Code under test runs through run_lab SCRIPT, run_lab_c CODE, or probe_split (labs/lib/lab.bash).

# shellcheck disable=SC2016 # expressions in run_lab_c expand in the shell under test

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/logging-probe.sh"
}

# probe_split OUT ERR MODE [ARG...]: run the probe under the shell under test with stdout and
# stderr in separate files; the exit status lands in probe_status (bats `run` merges the streams).
probe_split() {
  local out=$1 err=$2
  shift 2
  lab_shell_words
  probe_status=0
  "${LAB_SHELL_WORDS[@]}" "$PROBE" "$@" >"$out" 2>"$err" || probe_status=$?
}

# bash_at_least MAJOR MINOR: succeed when the bash under test is at least that version.
bash_at_least() {
  run_lab_c 'test "${BASH_VERSINFO[0]}" -gt "$0" || { test "${BASH_VERSINFO[0]}" -eq "$0" && test "${BASH_VERSINFO[1]}" -ge "$1"; }' "$1" "$2"
  [ "$status" -eq 0 ]
}

@test "data goes to stdout and every log line goes to stderr" {
  probe_split "$BATS_TEST_TMPDIR/out" "$BATS_TEST_TMPDIR/err" fd_clean
  [ "$probe_status" -eq 0 ]
  [ "$(wc -l <"$BATS_TEST_TMPDIR/out" | tr -d ' \t')" -eq 2 ]
  [ "$(wc -l <"$BATS_TEST_TMPDIR/err" | tr -d ' \t')" -eq 2 ]
  # A downstream parser sees only records: no stdout line is anything but a JSON object.
  [ "$(grep -c -v '^{.*}$' "$BATS_TEST_TMPDIR/out" || true)" -eq 0 ]
  # Every stderr line is a timestamped record carrying the pid of the writer.
  [ "$(grep -c -E '^2026-11-15T00:00:00Z \[INFO\] \[pid:[0-9]+\] ' "$BATS_TEST_TMPDIR/err")" -eq 2 ]
}

@test "log lines written to stdout contaminate the data stream a parser reads" {
  probe_split "$BATS_TEST_TMPDIR/out" "$BATS_TEST_TMPDIR/err" fd_polluted
  [ "$probe_status" -eq 0 ]
  [ "$(wc -l <"$BATS_TEST_TMPDIR/out" | tr -d ' \t')" -eq 4 ]
  [ "$(wc -c <"$BATS_TEST_TMPDIR/err" | tr -d ' \t')" -eq 0 ]
  # The same parser check as the clean test now finds two non-record lines.
  [ "$(grep -c -v '^{.*}$' "$BATS_TEST_TMPDIR/out")" -eq 2 ]
}

@test "a saved copy of stderr on fd 3 keeps logging after fd 2 is redirected away" {
  probe_split "$BATS_TEST_TMPDIR/out" "$BATS_TEST_TMPDIR/err" fd3_log
  [ "$probe_status" -eq 0 ]
  [ "$(cat "$BATS_TEST_TMPDIR/out")" = "data" ]
  [ "$(cat "$BATS_TEST_TMPDIR/err")" = "logged-on-fd3" ]
}

@test "printf %(...)T is a builtin in bash 4.2+ and ksh93 only" {
  family=$(lab_shell_family)
  run_lab "$PROBE" timestamp_builtin
  [ "$status" -eq 0 ]
  result=$output
  case $family in
    bash)
      if bash_at_least 4 2; then
        [ "$result" = "timestamp_engine=bash_builtin" ]
      else
        [ "$result" = "timestamp_engine=external_fork" ]
      fi
      ;;
    ksh93)
      [ "$result" = "timestamp_engine=ksh_builtin" ]
      ;;
    zsh | zsh-sh | dash | posh | mksh | yash | busybox)
      [ "$result" = "timestamp_engine=external_fork" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "the UTC timestamp is correct under a non-UTC local zone, via builtin or date" {
  family=$(lab_shell_family)
  pre=$(date -u +%Y-%m-%dT%H)
  run_lab "$PROBE" timestamp_now
  result=$output
  statusv=$status
  post=$(date -u +%Y-%m-%dT%H)
  [ "$statusv" -eq 0 ]
  case $result in
    engine=printf\ ts=[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z) ;;
    engine=date\ ts=[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z) ;;
    *) false ;;
  esac
  # The hour matches `date -u` read before or after the probe (the hour can roll over between).
  ts=${result#*ts=}
  [ "${ts%:*:*}" = "$pre" ] || [ "${ts%:*:*}" = "$post" ]
  case $family in
    bash)
      if bash_at_least 4 2; then
        [[ $result == engine=printf\ * ]]
      else
        [[ $result == engine=date\ * ]]
      fi
      ;;
    ksh93)
      [[ $result == engine=printf\ * ]]
      ;;
    zsh | zsh-sh | dash | posh | mksh | yash | busybox)
      [[ $result == engine=date\ * ]]
      ;;
    *)
      false
      ;;
  esac
}

@test "process substitution: parse, \$! and wait differ by shell and bash version" {
  family=$(lab_shell_family)
  posix=no
  # bash 5.1 made process substitution available in POSIX mode (bash-5.1 NEWS item u).
  if [ "$family" = bash ] && ! bash_at_least 5 1; then
    run_lab_c '[ -o posix ]'
    if [ "$status" -eq 0 ]; then
      posix=yes
    fi
  fi
  run_lab "$PROBE" procsub_wait
  [ "$status" -eq 0 ]
  result=$output
  case $family in
    bash)
      if [ "$posix" = yes ]; then
        # bash before 5.1 in POSIX mode (for example macOS /bin/sh) refuses process substitution.
        [ "$result" = "syntax=no" ]
      elif bash_at_least 4 4; then
        [ "$result" = "syntax=yes bang=set wait=0" ]
      elif bash_at_least 4 0; then
        # Before 4.4 the substitution has no job-table entry, so wait fails (non-zero).
        [[ $result == "syntax=yes bang="*" wait="[1-9]* ]]
      else
        # bash 3.2: $! is set but wait reports 127 ("not a child of this shell").
        [ "$result" = "syntax=yes bang=set wait=127" ]
      fi
      ;;
    zsh | zsh-sh)
      # zsh parses it natively and under sh emulation but never sets $! (it stays 0).
      [ "$result" = "syntax=yes bang=zero wait=127" ]
      ;;
    busybox)
      # BusyBox 1.37.0 ash runs the substitution but leaves $! unset, so wait "" fails.
      [[ $result == "syntax=yes bang=empty wait="[1-9]* ]]
      ;;
    dash | posh | mksh | yash)
      [ "$result" = "syntax=no" ]
      ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) [ "$result" = "syntax=yes bang=set wait=0" ] ;;
        *) [ "$result" = "syntax=no" ] ;;
      esac
      ;;
    *)
      false
      ;;
  esac
}

@test "O_APPEND keeps every concurrent record whole; a plain > open loses most of them" {
  appended="$BATS_TEST_TMPDIR/append.log"
  run_lab "$PROBE" append_race "$appended" append
  [ "$status" -eq 0 ]
  [ "$output" = "workers=4 lines_each=100" ]
  [ "$(wc -l <"$appended" | tr -d ' \t')" -eq 400 ]
  [ "$(grep -c -E '^w=0[1-4] seq=[0-9]{3} pad=x{20}$' "$appended")" -eq 400 ]
  for w in 1 2 3 4; do
    [ "$(grep -c "^w=0$w " "$appended")" -eq 100 ]
  done
  # Without O_APPEND each worker keeps its own offset from 0, so at most 100 line slots exist.
  truncated="$BATS_TEST_TMPDIR/trunc.log"
  run_lab "$PROBE" append_race "$truncated" trunc
  [ "$status" -eq 0 ]
  good=$(grep -c -E '^w=0[1-4] seq=[0-9]{3} pad=x{20}$' "$truncated" || true)
  [ "$good" -le 100 ]
}

@test "a failing producer piped into tee reports 0 unless pipefail is on" {
  run_lab "$PROBE" tee_status
  [ "$status" -eq 0 ]
  run_lab_c 'set -o pipefail'
  if [ "$status" -eq 0 ]; then
    run_lab "$PROBE" tee_status
    [ "$output" = "plain=0 pipefail=7" ]
  else
    run_lab "$PROBE" tee_status
    [ "$output" = "plain=0 pipefail=unsupported" ]
  fi
}

@test "logfmt leaves plain tokens bare and quotes anything with a space, quote or equals sign" {
  run_lab "$PROBE" format_logfmt INFO ok
  [ "$status" -eq 0 ]
  [[ $output == "ts=2026-11-15T00:00:00Z level=INFO pid="*" msg=ok" ]]
  run_lab "$PROBE" format_logfmt WARN 'cache "sync" done'
  [ "$status" -eq 0 ]
  [[ $output == "ts=2026-11-15T00:00:00Z level=WARN pid="*' msg="cache \"sync\" done"' ]]
  run_lab "$PROBE" format_logfmt ERROR 'a=b'
  [[ $output == *' msg="a=b"' ]]
  run_lab "$PROBE" format_logfmt ERROR ''
  [[ $output == *' msg=""' ]]
}

@test "JSON output escapes quote and backslash and stays on one line for control characters" {
  run_lab "$PROBE" format_json WARN 'say "hi" \ done'
  [ "$status" -eq 0 ]
  [[ $output == '{"ts":"2026-11-15T00:00:00Z","level":"WARN","pid":'* ]]
  [[ $output == *',"msg":"say \"hi\" \\ done"}' ]]
  # A newline, a carriage return, a tab and an ESC byte in the message cannot split the record.
  payload=$(printf 'a\nb\rc\td\033e')
  run_lab "$PROBE" format_json INFO "$payload"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 1 ]
  [[ $output == *',"msg":"a b c de"}' ]]
}

@test "the sd-daemon prefix carries the RFC 5424 severity computed from the level name" {
  n=0
  for level in emerg alert crit err warning notice info debug; do
    probe_split "$BATS_TEST_TMPDIR/out" "$BATS_TEST_TMPDIR/err" systemd_prefix "$level" "msg $level"
    [ "$probe_status" -eq 0 ]
    [ "$(cat "$BATS_TEST_TMPDIR/out")" = "priority=$n" ]
    [ "$(cat "$BATS_TEST_TMPDIR/err")" = "<$n>msg $level" ]
    n=$((n + 1))
  done
  probe_split "$BATS_TEST_TMPDIR/out" "$BATS_TEST_TMPDIR/err" systemd_prefix verbose x
  [ "$probe_status" -eq 2 ]
  [ "$(grep -c '^<' "$BATS_TEST_TMPDIR/err" || true)" -eq 0 ]
}

@test "log injection: raw input forges a second record and carries ESC; sanitized does not" {
  payload=$(printf 'normal\n[ERROR] forged\r\033[2Jx')
  run_lab "$PROBE" sanitize_cwe117 raw "$payload"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 2 ]
  [ "${lines[1]}" = "[ERROR] forged"$'\r\033[2Jx' ]
  run_lab "$PROBE" sanitize_cwe117 sanitized "$payload"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 1 ]
  [ "$output" = "2026-11-15T00:00:00Z [INFO] user_action=normal [ERROR] forged [2Jx" ]
}

@test "BASH_XTRACEFD moves the trace off stderr in bash 4.1+ and BusyBox ash only" {
  family=$(lab_shell_family)
  trace="$BATS_TEST_TMPDIR/trace.log"
  probe_split "$BATS_TEST_TMPDIR/out" "$BATS_TEST_TMPDIR/err" xtrace_separation "$trace"
  [ "$probe_status" -eq 0 ]
  [ "$(cat "$BATS_TEST_TMPDIR/out")" = "completed=xtrace" ]
  honored=no
  case $family in
    bash)
      if bash_at_least 4 1; then
        honored=yes
      fi
      ;;
    busybox)
      # BusyBox ash implements BASH_XTRACEFD as a bash-compatibility feature (measured, 1.37.0).
      honored=yes
      ;;
    zsh | zsh-sh | dash | posh | mksh | yash | ksh93) ;;
    *)
      false
      ;;
  esac
  if [ "$honored" = yes ]; then
    grep -q probe_traced_command "$trace"
    [ "$(wc -c <"$BATS_TEST_TMPDIR/err" | tr -d ' \t')" -eq 0 ]
  else
    [ "$(wc -c <"$trace" | tr -d ' \t')" -eq 0 ]
    grep -q probe_traced_command "$BATS_TEST_TMPDIR/err"
  fi
}
