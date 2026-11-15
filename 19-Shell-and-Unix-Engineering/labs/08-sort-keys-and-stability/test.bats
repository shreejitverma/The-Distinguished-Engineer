#!/usr/bin/env bats
# Lab: sort keys and stability (POSIX.1-2024 XCU sort).
# sort(1) behavior belongs to the sort implementation, not to the shell under test, so every
# test runs sort-probe.sh under the shell under test once per sort installed here and asserts
# that implementation's real behavior, branching on sort_flavor.

setup() {
  load ../lib/lab
}

# sort_impls: print each sort command installed here, one per line: the default sort, GNU
# gsort when present (macOS with Homebrew coreutils), and BusyBox sort when busybox has it.
sort_impls() {
  printf '%s\n' sort
  if command -v gsort >/dev/null 2>&1; then
    printf '%s\n' gsort
  fi
  if command -v busybox >/dev/null 2>&1 && busybox sort </dev/null >/dev/null 2>&1; then
    printf '%s\n' "busybox sort"
  fi
}

# sort_flavor CMD: print the implementation behind a sort command as gnu, bsd (FreeBSD and
# macOS), busybox, or unknown. BusyBox rejects --version and prints its banner on stderr.
sort_flavor() {
  local out
  # shellcheck disable=SC2086,SC2248 # CMD is one or two words ("busybox sort") by construction
  out=$($1 --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *BusyBox*) echo busybox ;;
    *-Apple* | *-FreeBSD*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# expect LINE...: the probe output must be exactly these lines.
expect() {
  local want
  want=$(printf '%s\n' "$@")
  if [ "$output" != "$want" ]; then
    printf 'impl: %s (%s)\nwant:\n%s\ngot:\n%s\n' "$impl" "$flavor" "$want" "$output" >&2
    return 1
  fi
}

# probe MODE: run the probe for MODE with the current $impl under the shell under test.
probe() {
  run_lab "$BATS_TEST_DIRNAME/sort-probe.sh" "$1" "$impl" </dev/null
  [ "$status" -eq 0 ]
}

@test "ties: equal keys fall back to a whole-line compare; -s keeps input order, even with -r" {
  local impl flavor impls count=0
  impls=$(sort_impls)
  while IFS= read -r impl; do
    count=$((count + 1))
    flavor=$(sort_flavor "$impl")
    probe ties
    case $flavor in
      gnu | bsd | busybox)
        expect \
          "tie: a 1|a 2|b 1|b 2|status=0" \
          "stable: a 1|a 2|b 2|b 1|status=0" \
          "reverse: b 2|b 1|a 2|a 1|status=0" \
          "stable-reverse: b 2|b 1|a 1|a 2|status=0" \
          "fold: A|a|B|b|status=0" \
          "fold-stable: a|A|b|B|status=0"
        ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$count" -ge 1 ]
}

@test "bounds: -k2 runs to end of line, leading blanks belong to the field, -t counts empty fields" {
  local impl flavor impls count=0
  impls=$(sort_impls)
  while IFS= read -r impl; do
    count=$((count + 1))
    flavor=$(sort_flavor "$impl")
    probe bounds
    case $flavor in
      gnu | bsd)
        expect \
          "open: y 1 a|x 1 z|status=0" \
          "closed: x 1 z|y 1 a|status=0" \
          "blanks: a  9|b 1|status=0" \
          "blanks-b: b 1|a  9|status=0" \
          "blanks-kb: b 1|a  9|status=0" \
          "sep-default: a  c|a b|status=0" \
          "sep-space: a b|a  c|status=0" \
          "char: q:za|p:zb|status=0"
        ;;
      busybox)
        # get_key() applies an end character offset from the start of the line, not of the
        # field, so -k2.2,2.2 is an empty key and -s keeps the input order.
        expect \
          "open: y 1 a|x 1 z|status=0" \
          "closed: x 1 z|y 1 a|status=0" \
          "blanks: a  9|b 1|status=0" \
          "blanks-b: b 1|a  9|status=0" \
          "blanks-kb: b 1|a  9|status=0" \
          "sep-default: a  c|a b|status=0" \
          "sep-space: a b|a  c|status=0" \
          "char: p:zb|q:za|status=0"
        ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$count" -ge 1 ]
}

@test "options: a key with any modifier inherits no global option" {
  local impl flavor impls count=0
  impls=$(sort_impls)
  while IFS= read -r impl; do
    count=$((count + 1))
    flavor=$(sort_flavor "$impl")
    probe options
    case $flavor in
      gnu | bsd | busybox)
        expect \
          "inherit: b 9|a 10|c 100|status=0" \
          "override: b 9|c 100|a 10|status=0"
        ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$count" -ge 1 ]
}

@test "numeric: -n reads only an initial decimal string; -g reads floating point" {
  local impl flavor impls count=0
  impls=$(sort_impls)
  while IFS= read -r impl; do
    count=$((count + 1))
    flavor=$(sort_flavor "$impl")
    probe numeric
    case $flavor in
      gnu | bsd)
        expect \
          "n-exp: 1e3|2|status=0" \
          "n-plus: +5|3|status=0" \
          "n-zero: -0|b|0|a|status=0" \
          "g-exp: 2|1e3|status=0"
        ;;
      busybox)
        # BusyBox -n is atof(): it accepts an exponent and a leading plus sign.
        expect \
          "n-exp: 2|1e3|status=0" \
          "n-plus: 3|+5|status=0" \
          "n-zero: -0|b|0|a|status=0" \
          "g-exp: 2|1e3|status=0"
        ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$count" -ge 1 ]
}

@test "unique: -u compares keys only; which duplicate survives is implementation-defined" {
  local impl flavor impls count=0
  impls=$(sort_impls)
  while IFS= read -r impl; do
    count=$((count + 1))
    flavor=$(sort_flavor "$impl")
    probe unique
    case $flavor in
      gnu | bsd)
        expect \
          "u-key: a 2|b 1|status=0" \
          "u-key-s: a 2|b 1|status=0" \
          "u-num: 1|status=0"
        ;;
      busybox)
        # BusyBox sorts with the whole-line tie-break first and then keeps the first line
        # of each run, so without -s the byte-smallest duplicate survives.
        expect \
          "u-key: a 2|b 0|status=0" \
          "u-key-s: a 2|b 1|status=0" \
          "u-num: 01|status=0"
        ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$count" -ge 1 ]
}

@test "check: -c reports disorder with status 1, -C is silent, a zero field is an error (2)" {
  local impl flavor impls count=0
  impls=$(sort_impls)
  while IFS= read -r impl; do
    count=$((count + 1))
    flavor=$(sort_flavor "$impl")
    probe check
    case $flavor in
      gnu | bsd)
        expect \
          "c-disorder: status=1 stderr=yes" \
          "c-sorted: status=0 stderr=no" \
          "C-disorder: status=1 stderr=no" \
          "cu-dup: status=1 stderr=yes" \
          "k-zero: status=2 stderr=yes"
        ;;
      busybox)
        # BusyBox has no -C: getopt32() prints the usage and exits 2.
        expect \
          "c-disorder: status=1 stderr=yes" \
          "c-sorted: status=0 stderr=no" \
          "C-disorder: status=2 stderr=yes" \
          "cu-dup: status=1 stderr=yes" \
          "k-zero: status=2 stderr=yes"
        ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$count" -ge 1 ]
}

@test "twopass: a stable second pass preserves the first pass's order; without -s it is lost" {
  local impl flavor impls count=0
  impls=$(sort_impls)
  while IFS= read -r impl; do
    count=$((count + 1))
    flavor=$(sort_flavor "$impl")
    probe twopass
    case $flavor in
      gnu | bsd | busybox)
        expect \
          "one-pass: a 9|a 10|b 2|b 10|status=0" \
          "two-pass-s: a 9|a 10|b 2|b 10" \
          "two-pass: a 10|a 9|b 10|b 2"
        ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$count" -ge 1 ]
}

@test "probe rejects an unknown mode with status 2" {
  run_lab "$BATS_TEST_DIRNAME/sort-probe.sh" nosuchmode sort </dev/null
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "usage: $BATS_TEST_DIRNAME/sort-probe.sh ties|bounds|options|numeric|unique|check|twopass SORTCMD" ]
}
