#!/usr/bin/env bats
# Tests for rg-probe.sh: ripgrep behaviors a script inherits that POSIX grep lacks.
# ripgrep's behavior does not depend on the shell under test; the shell only
# runs the probe, so most assertions hold for every family. The exception is
# the exit status the shell reports for grep killed by SIGPIPE.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/rg-probe.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

@test "ripgrep is installed (every other test needs it)" {
  run_lab_c 'command -v rg'
  [ "$status" -eq 0 ]
}

@test "alternation is leftmost-first in rg and leftmost-longest in grep" {
  run_lab "$PROBE" semantics
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "alternation rg=a rg_swapped=ab grep=ab" ]
}

@test "rg's \\w is Unicode-aware whatever LC_ALL says, and (?-u) makes it ASCII" {
  run_lab "$PROBE" semantics
  [ "$status" -eq 0 ]
  # c a f 0xC3 0xA9 newline is 6 bytes; c a f newline is 4.
  [ "${lines[1]}" = "unicode_word bytes=6 bytes_with_LC_ALL_C=6 ascii_only_bytes=4" ]
}

@test "dollar does not match before CR unless --crlf is given" {
  run_lab "$PROBE" semantics
  [ "$status" -eq 0 ]
  [ "${lines[2]}" = "crlf plain_status=1 plain_count=<> crlf_status=0 crlf_count=<1> count_line_has_cr=yes" ]
}

@test "a literal newline in the pattern is an error (status 2) unless -U is given" {
  run_lab "$PROBE" semantics
  [ "$status" -eq 0 ]
  [ "${lines[3]}" = "newline_in_pattern status=2 multiline_count=1" ]
}

@test "each -u removes one filter: ignore files, then hidden files, then binary files" {
  run_lab "$PROBE" filter
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "list none: a.txt sub/c.txt" ]
  [ "${lines[1]}" = "list -u: a.txt b.skipme sub/c.txt" ]
  [ "${lines[2]}" = "list -uu: .hid/h.txt a.txt b.skipme sub/c.txt" ]
  [ "${lines[3]}" = "list -uuu: .hid/h.txt a.txt b.skipme bin.dat sub/c.txt" ]
}

@test "an explicit path bypasses the hidden filter, and an explicit . adds a ./ prefix" {
  run_lab "$PROBE" filter
  [ "$status" -eq 0 ]
  [ "${lines[4]}" = "explicit_hidden: .hid/h.txt" ]
  [ "${lines[5]}" = "explicit_dot: ./a.txt ./sub/c.txt" ]
}

@test ".gitignore needs a .git (here or in a parent) unless --no-require-git; .ignore never does" {
  run_lab "$PROBE" filter
  [ "$status" -eq 0 ]
  # The probe reports whether its scratch directory sits inside a repository;
  # ripgrep counts a parent .git, so the expected default depends on that fact.
  case ${lines[6]} in
    *"ancestor_vcs=yes"*)
      [ "${lines[6]}" = "gitignore_without_git ancestor_vcs=yes default: a.txt; no-require-git: a.txt" ]
      ;;
    *"ancestor_vcs=no"*)
      [ "${lines[6]}" = "gitignore_without_git ancestor_vcs=no default: a.txt b.skipme; no-require-git: a.txt" ]
      ;;
    *) false ;;
  esac
  [ "${lines[7]}" = "dot_ignore_without_git: a.txt" ]
}

@test "a NUL in the first buffer fill hides a walked file but not an explicit one" {
  run_lab "$PROBE" binary
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "late_size=80026" ]
  [ "${lines[1]}" = "walked_early status=1 stdout_bytes=0" ]
  [ "${lines[2]}" = 'explicit_early status=0 stdout=binary file matches (found "\0" byte around offset 6)' ]
}

@test "a NUL after a match: the match prints with a warning, -l lists the file, -c reports nothing" {
  run_lab "$PROBE" binary
  [ "$status" -eq 0 ]
  [ "${lines[3]}" = "walked_late default status=0" ]
  [ "${lines[4]}" = "  | late.dat:needle early" ]
  [ "${lines[5]}" = '  | late.dat: WARNING: stopped searching binary file after match (found "\0" byte around offset 80013)' ]
  [ "${lines[6]}" = "walked_late -c status=1" ]
  [ "${lines[7]}" = "walked_late -l status=0" ]
  [ "${lines[8]}" = "  | late.dat" ]
  [ "${lines[9]}" = "walked_late --binary status=0" ]
  [ "${lines[10]}" = "  | late.dat:needle early" ]
  [ "${lines[11]}" = '  | late.dat: binary file matches (found "\0" byte around offset 80013)' ]
  [ "${lines[12]}" = "walked_late -a -c: late.dat:2" ]
}

@test "with no path, rg searches stdin only if it is a pipe or regular file" {
  run_lab "$PROBE" stdin
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "stdin_pipe: needle-from-pipe" ]
  [ "${lines[1]}" = "stdin_dev_null: f.txt:needle-in-file" ]
  [ "${lines[2]}" = "stdin_closed: f.txt:needle-in-file" ]
  [ "${lines[3]}" = "stdin_regular_file: needle-in-file" ]
  [ "${lines[4]}" = "explicit_dot: ./f.txt:needle-in-file" ]
}

@test "a bare rg in a while-read loop drains the loop's input; a path and </dev/null do not" {
  run_lab "$PROBE" stdin
  [ "$status" -eq 0 ]
  [ "${lines[5]}" = "while_read_loop iterations=1 with_path_and_dev_null=3" ]
}

@test "exit statuses follow grep: 1 no match, 2 any error even with a match, 0 for -q with a match" {
  run_lab "$PROBE" exit
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "rg no_match=1 match_and_error=2 quiet_match_and_error=0 bad_regex=2" ]
  [ "${lines[1]}" = "grep no_match=1 match_and_error=2 quiet_match_and_error=0 bad_regex=2" ]
  [ "${lines[2]}" = "nothing_searched status=2 stderr_lines=2" ]
}

@test "on a broken pipe rg exits quietly; the status says no match unless a parallel search saw one" {
  run_lab "$PROBE" pipe
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "output_bytes=2288895" ]
  [ "${lines[1]}" = "rg_one_file status=1 head_saw=<line 1>" ]
  [ "${lines[2]}" = "rg_dir_j1 status=1 head_saw=<d/big.txt:line 1>" ]
  [ "${lines[3]}" = "rg_dir_j2 status=0 head_saw=<d/big.txt:line 1>" ]
}

@test "grep on a broken pipe dies of SIGPIPE, reported per shell family" {
  run_lab "$PROBE" pipe
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | busybox | posh | mksh)
      [ "${lines[4]}" = "grep_one_file status=141 head_saw=<line 1>" ]
      ;;
    ksh93) [ "${lines[4]}" = "grep_one_file status=269 head_saw=<line 1>" ] ;;
    yash) [ "${lines[4]}" = "grep_one_file status=397 head_saw=<line 1>" ] ;;
    *) false ;;
  esac
}

@test "RIPGREP_CONFIG_PATH changes plain rg, even into running a program; --no-config stops it" {
  run_lab "$PROBE" config
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "config_ignore_case status=0 count=<1>" ]
  [ "${lines[1]}" = "no_config status=1 count=<>" ]
  [ "${lines[2]}" = "config_pre ran=yes count=<1>" ]
  [ "${lines[3]}" = "no_config_pre ran=no count=<1>" ]
}

@test "an unknown mode is rejected with status 2" {
  run_lab "$PROBE" no-such-mode
  [ "$status" -eq 2 ]
  [ "$output" = "rg-probe: unknown mode: no-such-mode" ]
}
