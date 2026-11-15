#!/usr/bin/env bats
# Tests for extsort.sh (a portable external merge sort) and probe.sh (how the
# sort(1) on PATH spills, merges, and rewrites files).
# sort's behavior depends on its implementation, not on the shell under test,
# so the probe tests branch on sort_flavor; extsort.sh must work in every shell.

setup() {
  load ../lib/lab
  EXTSORT="$BATS_TEST_DIRNAME/extsort.sh"
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

# sort_flavor: print gnu, apple, freebsd, uutils, busybox, or unknown for the sort on PATH.
sort_flavor() {
  local v
  v=$(sort --version 2>&1 </dev/null) || true
  case $v in
    *uutils*) echo uutils ;;
    *"GNU coreutils"*) echo gnu ;;
    *Apple*) echo apple ;;
    *FreeBSD*) echo freebsd ;;
    *BusyBox*) echo busybox ;;
    *) echo unknown ;;
  esac
}

# scrambled N FILE: write N distinct zero-padded lines in a scrambled order.
scrambled() {
  awk -v n="$1" 'BEGIN { for (i = 0; i < n; i++) printf "%05d\n", (i * 7919) % n }' >"$2"
}

@test "extsort.sh output is byte-identical to sort for 2000 lines in 200 runs" {
  scrambled 2000 "$BATS_TEST_TMPDIR/in"
  LC_ALL=C sort "$BATS_TEST_TMPDIR/in" >"$BATS_TEST_TMPDIR/want"
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 10 3 "$BATS_TEST_TMPDIR/in"
  [ "$status" -eq 0 ]
  [ "$output" = "runs=200 passes=5" ]
  cmp "$BATS_TEST_TMPDIR/want" "$BATS_TEST_TMPDIR/got"
}

@test "extsort.sh needs ceil(log_FANIN(runs)) merge passes for 10 runs" {
  scrambled 100 "$BATS_TEST_TMPDIR/in"
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 10 2 "$BATS_TEST_TMPDIR/in"
  [ "$output" = "runs=10 passes=4" ]
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 10 3 "$BATS_TEST_TMPDIR/in"
  [ "$output" = "runs=10 passes=3" ]
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 10 9 "$BATS_TEST_TMPDIR/in"
  [ "$output" = "runs=10 passes=2" ]
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 10 10 "$BATS_TEST_TMPDIR/in"
  [ "$output" = "runs=10 passes=1" ]
}

@test "extsort.sh writes to stdout without -o" {
  printf 'd\nb\nc\na\ne\n' >"$BATS_TEST_TMPDIR/in"
  run_lab "$EXTSORT" 2 2 "$BATS_TEST_TMPDIR/in"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'a\nb\nc\nd\ne')" ]
}

@test "extsort.sh: one run needs no merge pass and empty input gives empty output" {
  printf 'b\na\n' >"$BATS_TEST_TMPDIR/in"
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 10 2 "$BATS_TEST_TMPDIR/in"
  [ "$output" = "runs=1 passes=0" ]
  printf 'a\nb\n' >"$BATS_TEST_TMPDIR/want"
  cmp "$BATS_TEST_TMPDIR/want" "$BATS_TEST_TMPDIR/got"
  : >"$BATS_TEST_TMPDIR/empty"
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 10 2 "$BATS_TEST_TMPDIR/empty"
  [ "$status" -eq 0 ]
  [ "$output" = "runs=0 passes=0" ]
  [ ! -s "$BATS_TEST_TMPDIR/got" ]
}

@test "extsort.sh completes an incomplete last line, as POSIX requires of sort" {
  printf 'b\na' >"$BATS_TEST_TMPDIR/in"
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 1 2 "$BATS_TEST_TMPDIR/in"
  [ "$output" = "runs=2 passes=1" ]
  printf 'a\nb\n' >"$BATS_TEST_TMPDIR/want"
  cmp "$BATS_TEST_TMPDIR/want" "$BATS_TEST_TMPDIR/got"
}

@test "extsort.sh removes its work directory on success and on failure" {
  mkdir "$BATS_TEST_TMPDIR/t"
  export TMPDIR="$BATS_TEST_TMPDIR/t"
  scrambled 50 "$BATS_TEST_TMPDIR/in"
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 5 2 "$BATS_TEST_TMPDIR/in"
  [ "$status" -eq 0 ]
  left=$(ls -A "$BATS_TEST_TMPDIR/t")
  [ -z "$left" ]
  run_lab "$EXTSORT" -o "$BATS_TEST_TMPDIR/got" 5 2 "$BATS_TEST_TMPDIR/no-such-file"
  [ "$status" -ne 0 ]
  left=$(ls -A "$BATS_TEST_TMPDIR/t")
  [ -z "$left" ]
}

@test "extsort.sh rejects a fan-in below 2" {
  printf 'a\n' >"$BATS_TEST_TMPDIR/in"
  run_lab "$EXTSORT" 10 1 "$BATS_TEST_TMPDIR/in"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: extsort.sh [-o OUTPUT] RUN_LINES FANIN [FILE...]" ]
}

@test "a tiny -S makes sort spill to TMPDIR; BusyBox never spills" {
  run_lab "$PROBE" spill 1b
  [ "$status" -eq 0 ]
  case $(sort_flavor) in
    gnu | apple) [ "$output" = "status=2" ] ;;
    busybox) [ "$output" = "status=0" ] ;;
    *) false ;;
  esac
}

@test "a large -S keeps 200 lines in memory, so a missing TMPDIR is never touched" {
  run_lab "$PROBE" spill 1G
  [ "$status" -eq 0 ]
  case $(sort_flavor) in
    gnu | apple | busybox) [ "$output" = "status=0" ] ;;
    *) false ;;
  esac
}

@test "every temporary run is written once and read back once; a smaller fan-in writes more" {
  run_lab "$PROBE" temps 16
  [ "$status" -eq 0 ]
  wide=$output
  run_lab "$PROBE" temps 2
  [ "$status" -eq 0 ]
  narrow=$output
  case $(sort_flavor) in
    apple)
      # One run per line (200), then fan-in 16: 200 -> 13 -> output.
      [ "$wide" = "status=0 written=213 read=213 sorted=yes" ]
      # Fan-in 2: 200 -> 100 -> 50 -> 25 -> 13 -> 7 -> 4 -> 2 -> output.
      [ "$narrow" = "status=0 written=401 read=401 sorted=yes" ]
      ;;
    gnu)
      # Run length depends on the thread count and version; the invariants do not.
      w16=$(printf '%s\n' "$wide" | sed 's/.*written=\([0-9]*\) read=\([0-9]*\).*/\1 \2/')
      w2=$(printf '%s\n' "$narrow" | sed 's/.*written=\([0-9]*\) read=\([0-9]*\).*/\1 \2/')
      [ "${w16% *}" = "${w16#* }" ]
      [ "${w2% *}" = "${w2#* }" ]
      [ "${w16% *}" -gt 16 ]
      [ "${w2% *}" -gt "${w16% *}" ]
      case $wide$narrow in
        *sorted=no* | *status=[!0]*) false ;;
        *) true ;;
      esac
      ;;
    busybox)
      [ "$wide" = "status=1 written=0 read=0 sorted=yes" ]
      [ "$narrow" = "status=1 written=0 read=0 sorted=yes" ]
      ;;
    *) false ;;
  esac
}

@test "macOS sort truncates a long --compress-program command and exits 0 with no output" {
  run_lab "$PROBE" longprog
  [ "$status" -eq 0 ]
  case $(sort_flavor) in
    apple) [ "$output" = "status=0 lines=0" ] ;;
    gnu) [ "$output" = "status=0 lines=200" ] ;;
    busybox) [ "$output" = "status=1 lines=0" ] ;;
    *) false ;;
  esac
}

@test "stability survives the external merge: equal keys keep input order across runs" {
  awk 'BEGIN { for (i = 0; i < 60; i++) printf "k%d %03d\n", i % 3, 999 - i }' >"$BATS_TEST_TMPDIR/in"
  awk '{ print > (ENVIRON["BATS_TEST_TMPDIR"] "/want." substr($1, 2)) }' "$BATS_TEST_TMPDIR/in"
  cat "$BATS_TEST_TMPDIR/want.0" "$BATS_TEST_TMPDIR/want.1" "$BATS_TEST_TMPDIR/want.2" >"$BATS_TEST_TMPDIR/want"
  # shellcheck disable=SC2016 # $1 and $2 belong to the shell under test
  run_lab_c 'LC_ALL=C sort -s -k1,1 -S 1b "$1" >"$2"' sh "$BATS_TEST_TMPDIR/in" "$BATS_TEST_TMPDIR/got"
  [ "$status" -eq 0 ]
  cmp "$BATS_TEST_TMPDIR/want" "$BATS_TEST_TMPDIR/got"
}

@test "sort -m trusts its input: an unsorted file merges to unsorted output that -c rejects" {
  run_lab "$PROBE" merge-unsorted
  [ "$status" -eq 0 ]
  case $(sort_flavor) in
    gnu | apple) [ "$output" = "merged=b a c check=1" ] ;;
    busybox) [ "$output" = "merged=a b c check=1" ] ;;
    *) false ;;
  esac
}

@test "sort -o f f: GNU and BusyBox rewrite the same inode, macOS renames a new 0600 file" {
  run_lab "$PROBE" inplace
  [ "$status" -eq 0 ]
  case $(sort_flavor) in
    gnu | busybox) [ "$output" = "f=a b link=shared mode=-rw-r--r--" ] ;;
    apple) [ "$output" = "f=a b link=split mode=-rw-------" ] ;;
    *) false ;;
  esac
}

@test "sort -m -o ./x x y: macOS compares names, not inodes, and truncates x" {
  run_lab "$PROBE" merge-alias
  [ "$status" -eq 0 ]
  case $(sort_flavor) in
    gnu | busybox) [ "$output" = "x=a b c" ] ;;
    apple) [ "$output" = "x=b" ] ;;
    *) false ;;
  esac
}

@test "macOS sort follows a symlink planted at its predictable temporary name" {
  run_lab "$PROBE" predict
  [ "$status" -eq 0 ]
  case $(sort_flavor) in
    apple) [ "$output" = "victim=overwritten" ] ;;
    gnu | busybox) [ "$output" = "victim=intact" ] ;;
    *) false ;;
  esac
}

@test "GNU sort refuses a --batch-size above RLIMIT_NOFILE - 3 instead of shrinking it" {
  run_lab "$PROBE" batch-limit
  [ "$status" -eq 0 ]
  case $(sort_flavor) in
    gnu) [ "$output" = "$(printf 'status=2\nwith current rlimit is 17')" ] ;;
    apple) [ "$output" = "status=0" ] ;;
    busybox) [ "$output" = "status=1" ] ;;
    *) false ;;
  esac
}

@test "probe.sh rejects an unknown mode" {
  run_lab "$PROBE" no-such-mode
  [ "$status" -eq 2 ]
  [ "$output" = "usage: probe.sh MODE [ARG]" ]
}
