#!/usr/bin/env bats
# Tests for idioms.sh: every test loops over each installed awk (lab_awks) and asserts that
# implementation's behavior, because the shell under test never changes which awk runs.

setup() {
  load ../lib/lab
  S="$BATS_TEST_DIRNAME/idioms.sh"
  AWKS=$(lab_awks)
  [ -n "$AWKS" ]
}

# flavor_of CMD: lab_awk_flavor for a possibly multi-word awk command such as "busybox awk".
flavor_of() {
  local words
  IFS=' ' read -r -a words <<<"$1"
  lab_awk_flavor "${words[@]}"
}

# run_awk_mode AWKCMD MODE [ARG...]: run idioms.sh MODE under the shell under test, with AWK
# exported as AWKCMD for this call only.
run_awk_mode() {
  local AWK=$1
  export AWK
  shift
  run_lab "$S" "$@"
}

# each_awk MODE EXPECTED [FILE...]: run MODE under every awk and require EXPECTED output from each.
each_awk() {
  local mode=$1 expected=$2 a n=0
  shift 2
  while IFS= read -r a; do
    run_awk_mode "$a" "$mode" "$@"
    [ "$status" -eq 0 ]
    [ "$output" = "$expected" ]
    n=$((n + 1))
  done <<<"$AWKS"
  [ "$n" -gt 0 ]
}

@test "!seen[\$0]++ prints each distinct line once in first-seen order" {
  each_awk dedupe "$(printf 'b\na\nc')"
}

@test "c[\$1]++ counts per key; sorting the for-in output gives a stable report" {
  each_awk count "$(printf 'a 2\nb 2\nc 1')"
}

@test "a compound key seen[\$1, \$2] counts distinct values per group" {
  each_awk distinct "$(printf 'u1 2\nu2 1')"
}

@test "a side array order[++n] preserves first-seen key order, which for-in does not promise" {
  each_awk group "$(printf 'x 1,3\ny 2,5\nz 4')"
}

@test "m[NR, i] stores a matrix under SUBSEP keys and transposes it" {
  each_awk transpose "$(printf '1 4\n2 5\n3 6')"
}

@test "NR == FNR loads the first file into an array and joins the second against it" {
  printf '1 alice\n2 bob\n' >"$BATS_TEST_TMPDIR/names"
  printf 'o1 2\no2 3\no3 1\n' >"$BATS_TEST_TMPDIR/orders"
  each_awk join "$(printf 'o1 2 bob\no3 1 alice')" "$BATS_TEST_TMPDIR/names" "$BATS_TEST_TMPDIR/orders"
}

@test "with a non-empty first file the naive and robust anti-joins agree" {
  printf '1 alice\n2 bob\n' >"$BATS_TEST_TMPDIR/names"
  printf 'o1 2\no2 3\no3 1\n' >"$BATS_TEST_TMPDIR/orders"
  each_awk antijoin-naive "o2 3" "$BATS_TEST_TMPDIR/names" "$BATS_TEST_TMPDIR/orders"
  each_awk antijoin-robust "o2 3" "$BATS_TEST_TMPDIR/names" "$BATS_TEST_TMPDIR/orders"
}

@test "an empty first file makes NR == FNR swallow the second file; FILENAME == ARGV[1] does not" {
  : >"$BATS_TEST_TMPDIR/names"
  printf 'o1 2\no2 3\no3 1\n' >"$BATS_TEST_TMPDIR/orders"
  each_awk antijoin-naive "" "$BATS_TEST_TMPDIR/names" "$BATS_TEST_TMPDIR/orders"
  each_awk antijoin-robust "$(printf 'o1 2\no2 3\no3 1')" "$BATS_TEST_TMPDIR/names" "$BATS_TEST_TMPDIR/orders"
}

@test "an uninitialized variable as a subscript is the empty string, so l[0] is never stored" {
  each_awk reverse-bug "$(printf '[l3]\n[l2]\n[]')"
  each_awk reverse-fixed "$(printf '[l3]\n[l2]\n[l1]')"
}

@test "comparing against a missing element compares against 0 and loses all-negative maxima" {
  each_awk max-bug "c="
  each_awk max-fixed "c=-5"
}

@test "integral subscripts convert with %d, others through CONVFMT, strings stay as written" {
  each_awk keys "$(printf '0.3\n0.333333\n01\n1\n9007199254740992')"
}

@test "a huge integral subscript (1e30) maps to a different key string per implementation" {
  local a
  while IFS= read -r a; do
    run_awk_mode "$a" bigkey
    [ "$status" -eq 0 ]
    case $(flavor_of "$a") in
      gawk) [ "$output" = "1000000000000000019884624838656" ] ;;        # integer formatting of the exact double
      onetrue) [ "$output" = "1.00000000000000001988462483866e+30" ] ;; # "%.30g" in tran.c
      mawk) [ "$output" = "1e+30" ] ;;                                  # d_to_I clamps, so CONVFMT
      busybox) [ "$output" = "1e+30" ] ;;                               # n != (long long)n, so CONVFMT
      *) false ;;
    esac
  done <<<"$AWKS"
}

@test "changing CONVFMT after a store makes the same non-integer value miss its element" {
  local a
  while IFS= read -r a; do
    run_awk_mode "$a" convfmt
    [ "$status" -eq 0 ]
    case $(flavor_of "$a") in
      gawk | onetrue | mawk) [ "$output" = "$(printf 'missing 1\nstored=12.153')" ] ;;
      busybox) [ "$output" = "$(printf 'found 1\nstored=12.153')" ] ;; # getvar_s caches x's string (VF_CACHED)
      *) false ;;
    esac
  done <<<"$AWKS"
}

@test "referencing a missing element creates it; the in operator does not" {
  each_awk autoviv "empty 1 0"
}

@test "(i, j) joins with SUBSEP (default \\034), so a key containing SUBSEP collides" {
  each_awk subsep "1 1 2 1 2 1"
}

@test "elements added inside for-in: snapshot implementations skip them, onetrue awk visits some" {
  local a
  while IFS= read -r a; do
    run_awk_mode "$a" loop-add
    [ "$status" -eq 0 ]
    case $(flavor_of "$a") in
      gawk | mawk | busybox) [ "$output" = "visited=4 size=8" ] ;; # list built before the loop
      onetrue) [ "$output" = "visited=5 size=9" ] ;;               # walks the live hash chains
      *) false ;;
    esac
  done <<<"$AWKS"
}

@test "elements deleted inside for-in: snapshot implementations still visit their keys" {
  local a
  while IFS= read -r a; do
    run_awk_mode "$a" loop-delete
    [ "$status" -eq 0 ]
    case $(flavor_of "$a") in
      gawk | mawk | busybox) [ "$output" = "visited=3 live=1 size=1" ] ;;
      onetrue) [ "$output" = "visited=1 live=1 size=1" ] ;;
      *) false ;;
    esac
  done <<<"$AWKS"
}

@test "delete arr, split(\"\", arr), and element-wise delete all empty an array" {
  each_awk clear "0 0 0 0"
}

@test "arrays pass by reference, an untyped argument becomes an array, extra parameters are fresh" {
  each_awk byref "1 1 2 2"
}

@test "a NUL byte in a record: counted strings keep it, onetrue truncates, BusyBox ends the record" {
  local a
  while IFS= read -r a; do
    run_awk_mode "$a" nulkey
    [ "$status" -eq 0 ]
    case $(flavor_of "$a") in
      gawk | mawk) [ "$output" = 3 ] ;;
      onetrue) [ "$output" = 1 ] ;;
      busybox) [ "$output" = "$(printf '1\n1')" ] ;; # awk_getline: memchr finds the NUL as a record end
      *) false ;;
    esac
  done <<<"$AWKS"
}

@test "PROCINFO[\"sorted_in\"] orders for-in in gawk only; elsewhere the order is unspecified" {
  local a keys
  while IFS= read -r a; do
    run_awk_mode "$a" sorted-in
    [ "$status" -eq 0 ]
    case $(flavor_of "$a") in
      gawk) [ "$output" = "1 9 10" ] ;;
      onetrue | mawk | busybox)
        # No ordering promise: only the key set is checked (three keys, each present once).
        keys=" $output "
        [ "${#output}" -eq 6 ]
        case $keys in *" 1 "*) ;; *) false ;; esac
        case $keys in *" 9 "*) ;; *) false ;; esac
        case $keys in *" 10 "*) ;; *) false ;; esac
        ;;
      *) false ;;
    esac
  done <<<"$AWKS"
}

@test "an unknown mode is a usage error" {
  run_awk_mode awk no-such-mode
  [ "$status" -eq 2 ]
  [ "$output" = "idioms.sh: unknown mode: no-such-mode" ]
}
