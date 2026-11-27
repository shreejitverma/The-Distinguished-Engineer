#!/usr/bin/env bats
# Each test proves one review finding: the flawed form misbehaves, the reviewed form does not.
# Everything runs under the shell under test (LAB_SHELL) through run_lab or run_lab_c.
# shellcheck disable=SC2016 # the single-quoted code belongs to the shell under test

setup() {
  load ../lib/lab
  LAB=$BATS_TEST_DIRNAME
  export TMPDIR=$BATS_TEST_TMPDIR
}

# mk FILE TIMESTAMP: create an empty file and set its mtime (touch -t CCYYMMDDhhmm).
mk() {
  : >"$1"
  touch -t "$2" "$1"
}

# names DIR: set listing to the entries of DIR on one line, in glob order, for ordinary file names.
names() {
  listing=$(cd "$1" && printf '%s ' *)
}

# --- rotate: the five findings in the first draft ---------------------------------------------

@test "rotate-bad: a backup name with a space becomes two rm operands, so the old backup is never removed" {
  dir=$BATS_TEST_TMPDIR/backups
  mkdir "$dir"
  mk "$dir/backup-20261101 b.tar" 202611010000
  mk "$dir/backup-20261102.tar" 202611020000
  mk "$dir/backup-20261103.tar" 202611030000
  run_lab "$LAB/rotate-bad.sh" "$dir" 2
  [ "$status" -ne 0 ]
  [ -e "$dir/backup-20261101 b.tar" ]
  names "$dir"
  [ "$listing" = "backup-20261101 b.tar backup-20261102.tar backup-20261103.tar " ]
}

@test "rotate-bad: ls -t orders by mtime, so a restored older backup outranks the newer name" {
  dir=$BATS_TEST_TMPDIR/backups
  mkdir "$dir"
  # backup-20261102 is the newer backup by name but its file was restored with an older mtime.
  mk "$dir/backup-20261101.tar" 202611020000
  mk "$dir/backup-20261102.tar" 202611010000
  run_lab "$LAB/rotate-bad.sh" "$dir" 1
  [ "$status" -eq 0 ]
  names "$dir"
  [ "$listing" = "backup-20261101.tar " ]
}

@test "rotate-bad: an empty DIR makes cd go to HOME, and the rotation runs there" {
  home=$BATS_TEST_TMPDIR/home
  mkdir "$home"
  mk "$home/backup-20261101.tar" 202611010000
  mk "$home/backup-20261102.tar" 202611020000
  export HOME=$home
  run_lab "$LAB/rotate-bad.sh" "" 1
  [ "$status" -eq 0 ]
  names "$home"
  [ "$listing" = "backup-20261102.tar " ]
}

@test "rotate: name order decides, so a restored mtime cannot make it delete the newest backup" {
  dir=$BATS_TEST_TMPDIR/backups
  mkdir "$dir"
  mk "$dir/backup-20261101.tar" 202611020000
  mk "$dir/backup-20261102.tar" 202611010000
  run_lab "$LAB/rotate.sh" "$dir" 1
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  names "$dir"
  [ "$listing" = "backup-20261102.tar " ]
}

@test "rotate: a directory name with a space and a backup name with a space are both handled" {
  dir="$BATS_TEST_TMPDIR/my backups"
  mkdir "$dir"
  mk "$dir/backup-20261101 b.tar" 202611010000
  mk "$dir/backup-20261102.tar" 202611020000
  mk "$dir/backup-20261103.tar" 202611030000
  mk "$dir/notes.txt" 202611010000
  run_lab "$LAB/rotate.sh" "$dir" 2
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  names "$dir"
  [ "$listing" = "backup-20261102.tar backup-20261103.tar notes.txt " ]
}

@test "rotate: a backup name that contains a newline is removed whole" {
  dir=$BATS_TEST_TMPDIR/backups
  mkdir "$dir"
  nl=$(printf '\n_')
  nl=${nl%_}
  mk "$dir/backup-20261101${nl}x.tar" 202611010000
  mk "$dir/backup-20261102.tar" 202611020000
  run_lab "$LAB/rotate.sh" "$dir" 1
  [ "$status" -eq 0 ]
  names "$dir"
  [ "$listing" = "backup-20261102.tar " ]
}

@test "rotate: -n names exactly the files a real run removes and removes nothing itself" {
  dir="$BATS_TEST_TMPDIR/my backups"
  mkdir "$dir"
  mk "$dir/backup-20261101.tar" 202611010000
  mk "$dir/backup-20261102.tar" 202611020000
  mk "$dir/backup-20261103.tar" 202611030000
  run_lab "$LAB/rotate.sh" -n "$dir" 1
  [ "$status" -eq 0 ]
  [ "$output" = "would remove $dir/backup-20261101.tar
would remove $dir/backup-20261102.tar" ]
  names "$dir"
  [ "$listing" = "backup-20261101.tar backup-20261102.tar backup-20261103.tar " ]
  run_lab "$LAB/rotate.sh" "$dir" 1
  [ "$status" -eq 0 ]
  names "$dir"
  [ "$listing" = "backup-20261103.tar " ]
}

@test "rotate: KEEP larger than the file count removes nothing and KEEP 0 removes every backup" {
  dir=$BATS_TEST_TMPDIR/backups
  mkdir "$dir"
  mk "$dir/backup-20261101.tar" 202611010000
  mk "$dir/backup-20261102.tar" 202611020000
  run_lab "$LAB/rotate.sh" "$dir" 5
  [ "$status" -eq 0 ]
  names "$dir"
  [ "$listing" = "backup-20261101.tar backup-20261102.tar " ]
  run_lab "$LAB/rotate.sh" "$dir" 0
  [ "$status" -eq 0 ]
  contents=$(ls -A "$dir")
  [ -z "$contents" ]
}

@test "rotate: a KEEP that is not a plain decimal count is a usage error and removes nothing" {
  dir=$BATS_TEST_TMPDIR/backups
  mkdir "$dir"
  mk "$dir/backup-20261101.tar" 202611010000
  mk "$dir/backup-20261102.tar" 202611020000
  for keep in abc -1 08 '' 1.5 1234567890 '1 2'; do
    run_lab "$LAB/rotate.sh" "$dir" "$keep"
    [ "$status" -eq 2 ]
    names "$dir"
    [ "$listing" = "backup-20261101.tar backup-20261102.tar " ]
  done
}

@test "rotate: a wrong operand count and an empty DIR are usage errors, a missing DIR is a runtime failure" {
  run_lab "$LAB/rotate.sh" "$BATS_TEST_TMPDIR"
  [ "$status" -eq 2 ]
  run_lab "$LAB/rotate.sh" "" 1
  [ "$status" -eq 2 ]
  run_lab "$LAB/rotate.sh" "$BATS_TEST_TMPDIR/missing" 1
  [ "$status" -eq 1 ]
  [[ $output == *"not a directory"* ]]
}

@test "rotate: a directory with no backups is a no-op, except in native zsh where an unmatched glob is an error" {
  dir=$BATS_TEST_TMPDIR/backups
  mkdir "$dir"
  : >"$dir/notes.txt"
  run_lab "$LAB/rotate.sh" "$dir" 1
  case $(lab_shell_family) in
    zsh) [ "$status" -ne 0 ] && [[ $output == *"no matches found"* ]] ;;
    bash | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -eq 0 ]
      [ -z "$output" ]
      ;;
    *) false ;;
  esac
}

# --- the Steam launcher reduction ------------------------------------------------------------

@test "steamroot-bad: started by bare name, \${0%/*} is the whole name, cd fails, and the target becomes /*" {
  cd "$LAB"
  run_lab steamroot-bad.sh
  [ "$status" -eq 0 ]
  [[ $output == *"rm -rf /*" ]]
  last=${lines[${#lines[@]} - 1]}
  [ "$last" = "rm -rf /*" ]
}

@test "steamroot-bad: started with a path, \${0%/*} is the directory and the target is correct" {
  here=$(cd "$LAB" && pwd -P)
  run_lab "$LAB/steamroot-bad.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "rm -rf $here/*" ]
}

@test "steamroot: started by bare name or by path it targets the script's own directory" {
  here=$(cd "$LAB" && pwd -P)
  cd "$LAB"
  run_lab steamroot.sh
  [ "$status" -eq 0 ]
  [ "$output" = "rm -rf $here/*" ]
  run_lab "$LAB/steamroot.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "rm -rf $here/*" ]
}

@test "set -u does not catch an empty variable, but \${d:?} stops the shell before the command runs" {
  run_lab_c 'set -u; d=; printf "[%s]\n" "$d/"'
  [ "$status" -eq 0 ]
  [ "$output" = "[/]" ]
  # The subshell stands in for the script: an expansion error ends a subshell with a non-zero status (XCU 2.8.1).
  run_lab_c '(set -o noglob; d=; printf "%s\n" "${d:?}/"*; echo reached) 2>/dev/null; echo "inner=$?"'
  [[ $output == inner=* ]]
  [ "$output" != "inner=0" ]
  [[ $output != *reached* ]]
  [[ $output != *"/*"* ]]
}

# --- cd, options, and splitting --------------------------------------------------------------

@test "a failed cd does not stop the script, so the next command runs in the wrong directory" {
  mkdir "$BATS_TEST_TMPDIR/here"
  run_lab_c 'cd "$1"; cd missing; : >marker; echo "$?"' lab "$BATS_TEST_TMPDIR/here"
  [ -e "$BATS_TEST_TMPDIR/here/marker" ]
  run_lab_c 'cd "$1"; cd missing || exit 1; : >marker2' lab "$BATS_TEST_TMPDIR/here"
  [ "$status" -eq 1 ]
  [ ! -e "$BATS_TEST_TMPDIR/here/marker2" ]
}

@test "CDPATH redirects a relative cd and (except in native zsh) makes cd print the directory" {
  root=$BATS_TEST_TMPDIR
  mkdir -p "$root/cdp/sub" "$root/other/sub"
  cdp_target=$(cd "$root/cdp/sub" && pwd -P)
  other_target=$(cd "$root/other/sub" && pwd -P)
  run_lab_c 'CDPATH=$1; cd "$2"; d=$(cd sub && pwd -P); printf "%s\n" "$d"' lab "$root/cdp" "$root/other"
  [ "$status" -eq 0 ]
  [ "${lines[${#lines[@]} - 1]}" = "$cdp_target" ]
  case $(lab_shell_family) in
    zsh) [ "${#lines[@]}" -eq 1 ] ;;
    bash | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "${#lines[@]}" -eq 2 ] ;;
    *) false ;;
  esac
  run_lab_c 'CDPATH=$1; cd "$2"; d=$(cd ./sub && pwd -P); printf "%s\n" "$d"' lab "$root/cdp" "$root/other"
  [ "$status" -eq 0 ]
  [ "$output" = "$other_target" ]
}

@test "rm with an unquoted operand that starts with a hyphen reads it as options; -- ends option parsing" {
  dir=$BATS_TEST_TMPDIR/d
  mkdir "$dir"
  : >"$dir/-f"
  run_lab_c 'cd "$1" && f=$2 && rm $f' lab "$dir" -f
  [ -e "$dir/-f" ]
  run_lab_c 'cd "$1" && f=$2 && rm -- "$f"' lab "$dir" -f
  [ "$status" -eq 0 ]
  [ ! -e "$dir/-f" ]
}

@test "for f in \$(ls) splits a name with a space in two, a glob does not" {
  dir=$BATS_TEST_TMPDIR/d
  mkdir "$dir"
  : >"$dir/a b"
  : >"$dir/c"
  # shellcheck disable=SC2012 # the ls is the flaw this test demonstrates
  run_lab_c 'cd "$1"; n=0; for f in $(ls); do n=$((n + 1)); done; echo "$n"; n=0; for f in *; do n=$((n + 1)); done; echo "$n"' lab "$dir"
  [ "$status" -eq 0 ]
  [ "$output" = "3
2" ]
}

# --- errexit holes ---------------------------------------------------------------------------

@test "errexit: a function called as an if condition runs past a failing command" {
  run_lab "$LAB/errexit-holes.sh" if-context
  [ "$status" -eq 0 ]
  [ "$output" = "start
reached-in-f
cond-true" ]
}

@test "errexit: the left side of || is exempt too" {
  run_lab "$LAB/errexit-holes.sh" or-list
  [ "$status" -eq 0 ]
  [ "$output" = "start
reached-in-f
handled" ]
}

@test "errexit: an explicit || return inside the function makes it stop even as a condition" {
  run_lab "$LAB/errexit-holes.sh" fixed-if
  [ "$status" -eq 0 ]
  [ "$output" = "start
cond-false" ]
}

@test "errexit: a function whose last command is a failed && list kills the caller" {
  run_lab "$LAB/errexit-holes.sh" and-list-return
  [ "$status" -eq 1 ]
  [ "$output" = "start" ]
}

@test "errexit: a pipeline that begins with ! never aborts" {
  run_lab "$LAB/errexit-holes.sh" bang
  [ "$status" -eq 0 ]
  [ "$output" = "start
survived" ]
}

@test "errexit: a plain assignment takes the status of its command substitution and aborts" {
  run_lab "$LAB/errexit-holes.sh" assign
  [ "$status" -eq 1 ]
  [ "$output" = "start" ]
}

@test "errexit: export with a command substitution hides the failure, and the split form shows it" {
  run_lab "$LAB/errexit-holes.sh" export
  [ "$status" -eq 0 ]
  [ "$output" = "start
survived" ]
  run_lab "$LAB/errexit-holes.sh" export-fixed
  [ "$status" -eq 1 ]
  [ "$output" = "start" ]
}

@test "errexit: a failing stage in the middle of a pipeline is ignored" {
  run_lab "$LAB/errexit-holes.sh" pipeline
  [ "$status" -eq 0 ]
  [ "$output" = "start
survived" ]
}

@test "pipefail: without it the pipeline status is the last stage's, with it the failing stage's" {
  run_lab_c 'false | cat; echo "$?"'
  [ "$output" = 0 ]
  run_lab_c '(set -o pipefail) 2>/dev/null'
  if [ "$status" -eq 0 ]; then
    expected="start
status=1"
  else
    expected="start
no-pipefail"
  fi
  run_lab "$LAB/errexit-holes.sh" pipefail
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

@test "errexit-holes: an unknown mode is refused" {
  run_lab "$LAB/errexit-holes.sh" nonsense
  [ "$status" -eq 64 ]
}

# --- cleanup traps ---------------------------------------------------------------------------

@test "cleanup: with no trap the scratch directory is left behind" {
  run_lab "$LAB/cleanup-trap.sh" leak
  [ "$status" -eq 0 ]
  [ -d "${lines[0]}" ]
}

@test "cleanup: an EXIT trap removes it on a normal exit and on a set -e failure" {
  run_lab "$LAB/cleanup-trap.sh" normal
  [ "$status" -eq 0 ]
  [ -n "${lines[0]}" ] && [ ! -e "${lines[0]}" ]
  run_lab "$LAB/cleanup-trap.sh" fail
  [ "$status" -eq 1 ]
  [ -n "${lines[0]}" ] && [ ! -e "${lines[0]}" ]
}

@test "cleanup: a TERM trap that calls exit runs the EXIT trap and reports 143" {
  run_lab "$LAB/cleanup-trap.sh" term
  [ "$status" -eq 143 ]
  [ -n "${lines[0]}" ] && [ ! -e "${lines[0]}" ]
}

@test "cleanup: SIGKILL cannot be trapped, so the directory survives" {
  run_lab "$LAB/cleanup-trap.sh" kill
  [ "$status" -eq 137 ]
  [ -d "${lines[0]}" ]
}

@test "cleanup-trap: an unknown mode is refused and leaves no directory" {
  run_lab "$LAB/cleanup-trap.sh" nonsense
  [ "$status" -eq 64 ]
  contents=$(ls -A "$BATS_TEST_TMPDIR")
  [ -z "$contents" ]
}

# --- suppression audit -----------------------------------------------------------------------

@test "audit: reports each directive without a reason and counts every code" {
  f=$BATS_TEST_TMPDIR/sample.sh
  {
    printf '#!/bin/sh\n'
    printf '# shellcheck disable=SC2086\n'
    printf '  # shellcheck disable=SC2046,SC2248 # word splitting is the point here\n'
    printf '# shellcheck source=lib.sh\n'
    printf '# shellcheck shell=sh\n'
    printf '# see the shellcheck disable=SC1 note\n'
    printf '\t#shellcheck disable=SC2155 #\n'
    printf 'echo hi # shellcheck disable=SC2086\n'
    printf '# shellcheck disable=SC2154 # set by the caller'
  } >"$f"
  run_lab "$LAB/suppression-audit.sh" "$f"
  [ "$status" -eq 1 ]
  [ "$output" = "$f:2: no reason for SC2086
$f:7: no reason for SC2155
suppressions=5 unexplained=2" ]
}

@test "audit: a file whose directives all carry a reason passes, even without a final newline" {
  f=$BATS_TEST_TMPDIR/clean.sh
  {
    printf '# shellcheck disable=SC2086,SC2248 # unquoted on purpose\n'
    printf '# shellcheck disable=SC2154 # set by the caller'
  } >"$f"
  run_lab "$LAB/suppression-audit.sh" "$f"
  [ "$status" -eq 0 ]
  [ "$output" = "suppressions=3 unexplained=0" ]
}

@test "audit: -m makes the suppression count a ratchet" {
  f=$BATS_TEST_TMPDIR/clean.sh
  printf '# shellcheck disable=SC2086,SC2248 # unquoted on purpose\n' >"$f"
  run_lab "$LAB/suppression-audit.sh" -m 2 "$f"
  [ "$status" -eq 0 ]
  run_lab "$LAB/suppression-audit.sh" -m 1 "$f"
  [ "$status" -eq 1 ]
  [ "$output" = "suppressions=2 unexplained=0
suppressions=2 exceeds max=1" ]
}

@test "audit: bad usage and an unreadable file exit with 2" {
  run_lab "$LAB/suppression-audit.sh"
  [ "$status" -eq 2 ]
  run_lab "$LAB/suppression-audit.sh" -m x "$BATS_TEST_TMPDIR"
  [ "$status" -eq 2 ]
  run_lab "$LAB/suppression-audit.sh" "$BATS_TEST_TMPDIR/missing.sh"
  [ "$status" -eq 2 ]
}

@test "audit: every suppression in this lab carries a reason" {
  run_lab "$LAB/suppression-audit.sh" "$LAB"/*.sh "$LAB"/*.bats
  [ "$status" -eq 0 ]
  [[ $output == *" unexplained=0" ]]
}
