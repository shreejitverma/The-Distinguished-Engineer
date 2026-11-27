#!/usr/bin/env bats
# Tests for the cross-shell CI matrix lab: the matrix driver, the cell probe, and the matrix generator.
# shellcheck disable=SC2312 # each $(...) is compared against an exact value, so a failed read fails the test
# Every script runs under the shell under test (LAB_SHELL), so the driver is itself a matrix cell.

setup() {
  load ../lib/lab
  DIR=$BATS_TEST_DIRNAME
  export TMPDIR=$BATS_TEST_TMPDIR
}

# make_stub NAME: write an executable "shell" stub (a script that takes SCRIPT ARG... like a shell does).
make_stub() {
  printf '#!/bin/sh\n' >"$BATS_TEST_TMPDIR/$1"
  cat >>"$BATS_TEST_TMPDIR/$1"
  chmod +x "$BATS_TEST_TMPDIR/$1"
}

# out_lines: print the cell output lines (the "    out: " lines) of a -v run.
out_lines() {
  printf '%s\n' "$output" | sed -n 's/^    out: //p'
}

# fact KEY: print the value of KEY from a cell-facts.sh run held in $output.
fact() {
  printf '%s\n' "$output" | sed -n "s/^$1=//p"
}

# cell_line LABEL STATE RC: print the exact report line the driver prints for one cell.
cell_line() {
  printf '%-14s %-6s rc=%s' "$1" "$2" "$3"
}

# --- the driver -------------------------------------------------------------------------------

@test "driver: every passing cell is reported and the exit status is 0" {
  make_stub ok-a <<'EOS'
echo a
EOS
  make_stub ok-b <<'EOS'
echo b
EOS
  run_lab "$DIR/matrix-run.sh" -s "one=$BATS_TEST_TMPDIR/ok-a" -s "two=$BATS_TEST_TMPDIR/ok-b" /dev/null
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | grep -cxF "$(cell_line one pass 0)")" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -cxF "$(cell_line two pass 0)")" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -cxF 'matrix: 2 pass, 0 fail, 0 absent')" -eq 1 ]
}

@test "driver: a failing cell does not stop the cells after it, and the exit status is 1" {
  make_stub bad <<'EOS'
echo oops >&2
exit 3
EOS
  make_stub ok <<'EOS'
echo fine
EOS
  run_lab "$DIR/matrix-run.sh" -s "bad=$BATS_TEST_TMPDIR/bad" -s "ok=$BATS_TEST_TMPDIR/ok" /dev/null
  [ "$status" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -cxF "$(cell_line bad fail 3)")" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -cxF '    stderr: oops')" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -cxF "$(cell_line ok pass 0)")" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -cxF 'matrix: 1 pass, 1 fail, 0 absent')" -eq 1 ]
}

@test "driver: an absent shell is exit 3 without -a and tolerated with -a" {
  make_stub ok <<'EOS'
echo fine
EOS
  run_lab "$DIR/matrix-run.sh" -s "ok=$BATS_TEST_TMPDIR/ok" -s "gone=no-such-shell-for-the-lab" /dev/null
  [ "$status" -eq 3 ]
  [ "$(printf '%s\n' "$output" | grep -cxF "$(cell_line gone absent -)")" -eq 1 ]
  run_lab "$DIR/matrix-run.sh" -a -s "ok=$BATS_TEST_TMPDIR/ok" -s "gone=no-such-shell-for-the-lab" /dev/null
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | grep -cxF 'matrix: 1 pass, 0 fail, 1 absent')" -eq 1 ]
}

@test "driver: a matrix in which nothing ran is never green, even with -a" {
  run_lab "$DIR/matrix-run.sh" -a -s "gone=no-such-shell-for-the-lab" /dev/null
  [ "$status" -eq 3 ]
  [ "$(printf '%s\n' "$output" | grep -cxF 'matrix: 0 pass, 0 fail, 1 absent')" -eq 1 ]
}

@test "driver: shell arguments, then the script, then script arguments reach the cell in order" {
  make_stub args <<'EOS'
for a in "$@"; do
  printf '<%s>\n' "$a"
done
EOS
  run_lab "$DIR/matrix-run.sh" -v -s "args=$BATS_TEST_TMPDIR/args --flag two" /the/script x 'y z' ''
  [ "$status" -eq 0 ]
  [ "$(out_lines)" = "$(printf '<--flag>\n<two>\n</the/script>\n<x>\n<y z>\n<>')" ]
}

@test "driver: -e turns a cell whose output differs into a differ failure" {
  make_stub says-x <<'EOS'
echo x
EOS
  printf 'x\n' >"$BATS_TEST_TMPDIR/expect-x"
  printf 'y\n' >"$BATS_TEST_TMPDIR/expect-y"
  run_lab "$DIR/matrix-run.sh" -e "$BATS_TEST_TMPDIR/expect-x" -s "c=$BATS_TEST_TMPDIR/says-x" /dev/null
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | grep -cxF "$(cell_line c pass 0)")" -eq 1 ]
  run_lab "$DIR/matrix-run.sh" -e "$BATS_TEST_TMPDIR/expect-y" -s "c=$BATS_TEST_TMPDIR/says-x" /dev/null
  [ "$status" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -cxF "$(cell_line c differ 0)")" -eq 1 ]
  [ "$(printf '%s\n' "$output" | grep -cxF 'matrix: 0 pass, 1 fail, 0 absent')" -eq 1 ]
}

@test "driver: -i scrubs the environment down to PATH and -E adds variables after the scrub" {
  make_stub env-probe <<'EOS'
printf 'poison=%s\n' "${MATRIX_POISON-unset}"
printf 'path=%s\n' "${PATH:+set}"
EOS
  export MATRIX_POISON=1
  run_lab "$DIR/matrix-run.sh" -v -s "e=$BATS_TEST_TMPDIR/env-probe" /dev/null
  [ "$(out_lines)" = "$(printf 'poison=1\npath=set')" ]
  run_lab "$DIR/matrix-run.sh" -v -i -s "e=$BATS_TEST_TMPDIR/env-probe" /dev/null
  [ "$(out_lines)" = "$(printf 'poison=unset\npath=set')" ]
  run_lab "$DIR/matrix-run.sh" -v -i -E MATRIX_POISON=2 -s "e=$BATS_TEST_TMPDIR/env-probe" /dev/null
  [ "$(out_lines)" = "$(printf 'poison=2\npath=set')" ]
}

@test "driver: cells read standard input from /dev/null, not from the driver's own files" {
  make_stub reads-stdin <<'EOS'
n=$(cat | wc -c)
echo "stdin-bytes=$((n))"
EOS
  run_lab "$DIR/matrix-run.sh" -v -s "a=$BATS_TEST_TMPDIR/reads-stdin" -s "b=$BATS_TEST_TMPDIR/reads-stdin" /dev/null
  [ "$status" -eq 0 ]
  [ "$(out_lines)" = "$(printf 'stdin-bytes=0\nstdin-bytes=0')" ]
}

@test "driver: its scratch directory is removed after a run and after a usage error" {
  make_stub ok <<'EOS'
echo fine
EOS
  run_lab "$DIR/matrix-run.sh" -s "ok=$BATS_TEST_TMPDIR/ok" /dev/null
  [ "$status" -eq 0 ]
  run_lab "$DIR/matrix-run.sh" -s "ok=$BATS_TEST_TMPDIR/ok"
  [ "$status" -eq 2 ]
  [ -z "$(find "$BATS_TEST_TMPDIR" -maxdepth 1 -name 'matrix-run.*')" ]
}

@test "driver: usage errors exit 2 and name the problem" {
  run_lab "$DIR/matrix-run.sh" /dev/null
  [ "$status" -eq 2 ]
  [[ $output == *"no cell given"* ]]
  run_lab "$DIR/matrix-run.sh" -s 'bad label=sh' /dev/null
  [ "$status" -eq 2 ]
  [[ $output == *"a cell label must match"* ]]
  run_lab "$DIR/matrix-run.sh" -s nolabel /dev/null
  [ "$status" -eq 2 ]
  [[ $output == *"-s wants LABEL=COMMAND"* ]]
  run_lab "$DIR/matrix-run.sh" -E 1BAD=x -s a=sh /dev/null
  [ "$status" -eq 2 ]
  [[ $output == *"-E wants NAME=VALUE"* ]]
  run_lab "$DIR/matrix-run.sh" -e "$BATS_TEST_TMPDIR/does-not-exist" -s a=sh /dev/null
  [ "$status" -eq 2 ]
  [[ $output == *"cannot read the expected-output file"* ]]
  run_lab "$DIR/matrix-run.sh" -z -s a=sh /dev/null
  [ "$status" -eq 2 ]
}

@test "driver: a real cell runs the probe under the shell under test" {
  run_lab "$DIR/matrix-run.sh" -v -s "self=${LAB_SHELL:-sh}" "$DIR/cell-facts.sh"
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | grep -cxF "$(cell_line self pass 0)")" -eq 1 ]
  [ "$(out_lines | grep -c '^[a-z_]*=')" -eq 5 ]
}

@test "driver: one bash binary is two cells when it is invoked under two names" {
  bash_path=$(command -v bash)
  ln -s "$bash_path" "$BATS_TEST_TMPDIR/bash"
  ln -s "$bash_path" "$BATS_TEST_TMPDIR/sh"
  run_lab "$DIR/matrix-run.sh" -v -s "as-bash=$BATS_TEST_TMPDIR/bash" -s "as-sh=$BATS_TEST_TMPDIR/sh" "$DIR/bash-mode.sh"
  [ "$status" -eq 0 ]
  [ "$(out_lines)" = "$(printf 'posix=off\nposix=on')" ]
}

# --- the probe --------------------------------------------------------------------------------

@test "probe: an unquoted expansion is split into two fields, except in native zsh" {
  run_lab "$DIR/cell-facts.sh"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh) [ "$(fact split_count)" = 1 ] ;;
    bash | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "$(fact split_count)" = 2 ] ;;
    *) false ;;
  esac
}

@test "probe: a dollar-single-quote word expands except in dash 0.5.12 and posh" {
  run_lab "$DIR/cell-facts.sh"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    posh) [ "$(fact dollar_squote)" = '$\x41' ] ;;
    # dash 0.5.12 (Debian 13) keeps '$\x41'; the dash 0.5.13 series expands it; the shell reports no version
    dash) [ "$(fact dollar_squote)" = A ] || [ "$(fact dollar_squote)" = '$\x41' ] ;;
    bash | zsh | zsh-sh | yash | busybox | mksh | ksh93) [ "$(fact dollar_squote)" = A ] ;;
    *) false ;;
  esac
}

@test "probe: the status of a SIGKILLed child is 128+9, 256+9 in ksh93 and 384+9 in yash" {
  run_lab "$DIR/cell-facts.sh"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    ksh93) [ "$(fact sigkill_status)" = 265 ] ;;
    yash) [ "$(fact sigkill_status)" = 393 ] ;;
    bash | zsh | zsh-sh | busybox | posh | mksh | dash) [ "$(fact sigkill_status)" = 137 ] ;;
    *) false ;;
  esac
}

@test "probe: pipefail is reported exactly when set -o pipefail is accepted, and then it works" {
  run_lab_c 'set -o pipefail'
  direct=$status
  run_lab "$DIR/cell-facts.sh"
  [ "$status" -eq 0 ]
  if [ "$direct" -eq 0 ]; then
    [ "$(fact pipefail)" = yes/rc=1 ]
  else
    [ "$(fact pipefail)" = no ]
  fi
}

@test "probe: IFS at startup is the default, never the caller's, except in posh (-E IFS=: injected)" {
  run_lab "$DIR/matrix-run.sh" -v -s "self=${LAB_SHELL:-sh}" "$DIR/cell-facts.sh"
  [ "$status" -eq 0 ]
  baseline=$(out_lines | sed -n 's/^ifs_hex=//p')
  run_lab "$DIR/matrix-run.sh" -v -E IFS=: -s "self=${LAB_SHELL:-sh}" "$DIR/cell-facts.sh"
  [ "$status" -eq 0 ]
  injected=$(out_lines | sed -n 's/^ifs_hex=//p')
  case $(lab_shell_family) in
    zsh)
      [ "$baseline" = 20090a00 ]
      [ "$injected" = 20090a00 ]
      ;;
    posh)
      [ "$baseline" = 20090a ]
      [ "$injected" = 3a ]
      ;;
    bash | zsh-sh | yash | busybox | mksh | ksh93 | dash)
      [ "$baseline" = 20090a ]
      [ "$injected" = 20090a ]
      ;;
    *) false ;;
  esac
}

# --- a second axis: the utilities do not change with the shell ----------------------------------

@test "utilities: awk paragraph mode with FS=: depends on the awk, not on the shell" {
  seen=0
  while IFS= read -r cmd; do
    read -r -a words <<<"$cmd"
    flavor=$(lab_awk_flavor "${words[@]}")
    got=$(printf 'a:b\nc:d\n' | "${words[@]}" 'BEGIN { RS = ""; FS = ":" } { print NF }')
    case $flavor in
      mawk) [ "$got" = 3 ] ;;
      gawk | onetrue | busybox) [ "$got" = 4 ] ;;
      *) false ;;
    esac
    seen=$((seen + 1))
  done < <(lab_awks)
  [ "$seen" -ge 1 ]
}

# --- the generator ----------------------------------------------------------------------------

@test "generator: a list becomes the fromJSON matrix, skipping comments and blank lines" {
  printf '# shells\ndash debian:13 dash\n\nbusybox-ash alpine:3.22 busybox,bash\n' >"$BATS_TEST_TMPDIR/list"
  run_lab "$DIR/gen-matrix.sh" "$BATS_TEST_TMPDIR/list"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = '{"include":[{"shell":"dash","image":"debian:13","packages":"dash"},{"shell":"busybox-ash","image":"alpine:3.22","packages":"busybox,bash"}]}' ]
  [ "${lines[1]}" = '2 cells' ]
}

@test "generator: 256 cells are accepted and 257 are refused" {
  awk 'BEGIN { for (i = 1; i <= 256; i++) printf "s%d img pkg\n", i }' >"$BATS_TEST_TMPDIR/l256"
  awk 'BEGIN { for (i = 1; i <= 257; i++) printf "s%d img pkg\n", i }' >"$BATS_TEST_TMPDIR/l257"
  run_lab "$DIR/gen-matrix.sh" "$BATS_TEST_TMPDIR/l256"
  [ "$status" -eq 0 ]
  [ "$(printf '%s\n' "$output" | grep -o '"shell"' | wc -l | tr -d ' ')" -eq 256 ]
  [ "$(printf '%s\n' "$output" | grep -cxF '256 cells')" -eq 1 ]
  run_lab "$DIR/gen-matrix.sh" "$BATS_TEST_TMPDIR/l257"
  [ "$status" -eq 1 ]
  [[ $output == *"257 cells exceed the limit of 256 per matrix"* ]]
}

@test "generator: malformed lists are refused with a reason" {
  printf 'dash debian:13 dash\ndash alpine:3 dash\n' >"$BATS_TEST_TMPDIR/dup"
  run_lab "$DIR/gen-matrix.sh" "$BATS_TEST_TMPDIR/dup"
  [ "$status" -eq 1 ]
  [[ $output == *"NAME 'dash' appears twice"* ]]
  printf 'a"b debian:13 dash\n' >"$BATS_TEST_TMPDIR/quote"
  run_lab "$DIR/gen-matrix.sh" "$BATS_TEST_TMPDIR/quote"
  [ "$status" -eq 1 ]
  [[ $output == *"bad NAME"* ]]
  printf 'dash debian:13\n' >"$BATS_TEST_TMPDIR/short"
  run_lab "$DIR/gen-matrix.sh" "$BATS_TEST_TMPDIR/short"
  [ "$status" -eq 1 ]
  [[ $output == *"needs NAME IMAGE PACKAGES"* ]]
  printf 'dash debian:13 dash extra\n' >"$BATS_TEST_TMPDIR/long"
  run_lab "$DIR/gen-matrix.sh" "$BATS_TEST_TMPDIR/long"
  [ "$status" -eq 1 ]
  [[ $output == *"more than three words"* ]]
  # shellcheck disable=SC2016 # a literal dollar sign is the point: it must be refused
  printf 'dash debian:13 da$h\n' >"$BATS_TEST_TMPDIR/pkg"
  run_lab "$DIR/gen-matrix.sh" "$BATS_TEST_TMPDIR/pkg"
  [ "$status" -eq 1 ]
  [[ $output == *"bad PACKAGES"* ]]
  printf '# nothing\n\n' >"$BATS_TEST_TMPDIR/empty"
  run_lab "$DIR/gen-matrix.sh" "$BATS_TEST_TMPDIR/empty"
  [ "$status" -eq 1 ]
  [[ $output == *"no cells in"* ]]
}
