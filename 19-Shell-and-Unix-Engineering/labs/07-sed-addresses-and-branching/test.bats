#!/usr/bin/env bats
# sed addresses and branching: POSIX-required behavior in every sed here, and the
# per-implementation divergences (GNU, BSD and macOS, BusyBox) of ranges, labels, and t.
# The shell under test runs the scripts, but it never changes which sed runs, so the
# expectations branch on the sed implementation (sed-flavor.sh), not on the shell family.

setup() {
  load ../lib/lab
  ADDRESSES="$BATS_TEST_DIRNAME/addresses.sh"
  PROBE="$BATS_TEST_DIRNAME/sed-probe.sh"
  FLAVOR="$BATS_TEST_DIRNAME/sed-flavor.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
  SEDS=()
  local cmd
  for cmd in sed gsed; do
    if command -v "$cmd" >/dev/null 2>&1; then
      SEDS+=("$cmd")
    fi
  done
  if command -v busybox >/dev/null 2>&1 && busybox sed --version >/dev/null 2>&1; then
    SEDS+=("busybox sed")
  fi
}

# expect_probe CASE GNU BSD BUSYBOX: run sed-probe.sh CASE under the shell under test with
# every sed installed here, and compare its one line of output with the line expected for
# that sed's implementation. An implementation not listed fails the test.
expect_probe() {
  local case_name=$1 want_gnu=$2 want_bsd=$3 want_busybox=$4
  local sed_cmd flavor want checked=0
  local -a words
  for sed_cmd in "${SEDS[@]}"; do
    IFS=' ' read -r -a words <<<"$sed_cmd"
    flavor=$(sh "$FLAVOR" "${words[@]}")
    case $flavor in
      gnu) want=$want_gnu ;;
      bsd) want=$want_bsd ;;
      busybox) want=$want_busybox ;;
      *)
        echo "unexpected sed implementation '$flavor' for '$sed_cmd'"
        return 1
        ;;
    esac
    run_lab "$PROBE" "$case_name" "${words[@]}"
    if [ "$status" -ne 0 ] || [ "$output" != "$want" ]; then
      echo "$case_name with '$sed_cmd' ($flavor): status $status, got '$output', want '$want'"
      return 1
    fi
    checked=$((checked + 1))
  done
  [ "$checked" -gt 0 ]
}

@test "addresses.sh: POSIX ranges, negation, loops, and file spanning agree in every sed" {
  local sed_cmd checked=0
  local -a words
  for sed_cmd in "${SEDS[@]}"; do
    IFS=' ' read -r -a words <<<"$sed_cmd"
    run_lab "$ADDRESSES" "${words[@]}"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 12 ]
    [ "${lines[0]}" = "range 2,4: 2|3|4" ]
    [ "${lines[1]}" = "range 4,2: 4" ]
    [ "${lines[2]}" = "range /x/,/x/: x|a|x|x|c" ]
    [ "${lines[3]}" = "keep blocks: BEGIN|x|y|END" ]
    [ "${lines[4]}" = "first match only: K=1|x|k=2|k=3" ]
    [ "${lines[5]}" = "join continuations: a b c|d" ]
    [ "${lines[6]}" = "thousands: 1,234,567|12" ]
    [ "${lines[7]}" = "slurp: a+b+c" ]
    [ "${lines[8]}" = "slurp 1 line: a" ]
    [ "${lines[9]}" = "two files: b|c|d" ]
    [ "${lines[10]}" = "c on 2,3: 1|X|4" ]
    [ "${lines[11]}" = "c on 3!: X|X|3" ]
    checked=$((checked + 1))
  done
  [ "$checked" -gt 0 ]
}

@test "a numeric first address that a branch jumped over: GNU and BusyBox open the range late, BSD never" {
  expect_probe skipped-start 'out=3|4 st=0' 'out= st=0' 'out=3|4 st=0'
}

@test "a numeric second address that a branch jumped over closes the range in GNU and BSD; BusyBox selects one more line" {
  expect_probe jumped-end 'out=5|6|7|8|9|10|11|12 st=0' 'out=5|6|7|8|9|10|11|12 st=0' 'out=5|6|7|8|10|11|12 st=0'
}

@test "0,/re/ ends on line 1 only in GNU sed" {
  expect_probe zero-range 'out=x st=0' 'out= st=0' 'out= st=0'
}

@test "line address 0 alone: GNU rejects it, BSD never matches it, BusyBox treats it as no address" {
  expect_probe zero-alone 'out= st=1' 'out= st=0' 'out=1|2 st=0'
}

@test "a label ends at a semicolon in GNU and BusyBox, at the newline in BSD" {
  expect_probe semicolon-label 'out=a+b+c st=0' 'out=a|b|c st=0' 'out=a+b+c st=0'
}

@test "a closing brace directly after a command: GNU and BusyBox accept it, BSD rejects it" {
  expect_probe brace-close 'out=1 st=0' 'out= st=1' 'out=1 st=0'
}

@test "a doubled ! is an error in GNU and BusyBox and a single negation in BSD" {
  expect_probe double-bang 'out= st=1' 'out=1|3 st=0' 'out= st=1'
}

@test "N clears the t flag in GNU and BSD, as POSIX requires; BusyBox keeps it" {
  expect_probe t-after-N 'out=X|b NO st=0' 'out=X|b NO st=0' 'out=X|b st=0'
}

@test "N with no next line prints the pattern space in GNU and BusyBox, not in BSD" {
  expect_probe n-eof 'out=a+b|c st=0' 'out=a+b st=0' 'out=a+b|c st=0'
}

@test "POSIXLY_CORRECT makes GNU N at end of input quit without printing; BusyBox ignores it" {
  expect_probe n-eof-posix 'out=a+b st=0' 'out=a+b st=0' 'out=a+b|c st=0'
}

@test "a duplicate label: GNU jumps to the last one, BusyBox to the first, BSD rejects the script" {
  expect_probe dup-label 'out=2a st=0' 'out= st=1' 'out=21a st=0'
}

@test "an undefined label in a branch that never runs: GNU (status 4) and BSD fail at compile time, BusyBox runs" {
  expect_probe unreached-label 'out= st=4' 'out= st=1' 'out=a st=0'
}

@test "first~step is GNU only" {
  expect_probe step 'out=1|3|5 st=0' 'out= st=1' 'out= st=1'
}

@test "addr1,+N works in GNU, BSD, and BusyBox" {
  expect_probe plus 'out=2|3 st=0' 'out=2|3 st=0' 'out=2|3 st=0'
}

@test "addr1,~N is GNU only" {
  expect_probe tilde 'out=2|3|4 st=0' 'out= st=1' 'out= st=1'
}

@test "T branches when no substitution succeeded in GNU and BusyBox; BSD has no T" {
  expect_probe T 'out=X!|b st=0' 'out= st=1' 'out=X!|b st=0'
}

@test "c-backslash text continued into the next -e option works in GNU and BusyBox, not in BSD" {
  expect_probe c-split-e 'out=1|X|4 st=0' 'out= st=1' 'out=1|X|4 st=0'
}

@test "a 2048-byte script line with one context address works in every sed" {
  expect_probe address-2048 'out=1 st=0' 'out=1 st=0' 'out=1 st=0'
}

@test "a 2049-byte script line overflows the BSD 2048-byte line buffer and splits the address" {
  expect_probe address-2049 'out=1 st=0' 'out= st=1' 'out=1 st=0'
}

@test "the I flag on a context address works in GNU and BSD, not in BusyBox" {
  expect_probe icase 'out=Foo st=0' 'out=Foo st=0' 'out= st=1'
}

@test "sed-probe.sh rejects an unknown case and a missing case name with status 2" {
  run_lab "$PROBE" no-such-case sed
  [ "$status" -eq 2 ]
  [ "$output" = "sed-probe.sh: unknown case: no-such-case" ]
  run_lab "$PROBE"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: sed-probe.sh CASE [SED [ARG...]]" ]
}
