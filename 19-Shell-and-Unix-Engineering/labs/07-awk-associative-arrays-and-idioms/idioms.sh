#!/bin/sh
# shellcheck disable=SC2016 # every awk program is single-quoted so awk, not the shell, expands it
# awk associative arrays: subscript conversion, iteration under mutation, and the classic idioms.
# Usage: AWK='busybox awk' idioms.sh MODE [FILE...]
# AWK names the awk command (default: awk); the shell under test never changes which awk runs.
set -eu

[ "$#" -ge 1 ] || {
  echo "usage: idioms.sh MODE [FILE...]" >&2
  exit 2
}
mode=$1
shift

# run_awk ARG...: run the awk named by AWK (one or more words, for example "busybox awk").
# eval splits AWK in every shell, including native zsh, which does not split unquoted expansions.
run_awk() {
  eval "${AWK:-awk}"' "$@"'
}

case $mode in
  dedupe)
    # Print each distinct line once, in first-seen order.
    printf 'b\na\nb\nc\na\n' | run_awk '!seen[$0]++'
    ;;
  count)
    # Count occurrences per key; for-in order is unspecified, so sort for a stable report.
    printf 'b\na\nb\nc\na\n' | run_awk '{ c[$1]++ } END { for (k in c) print k, c[k] }' | LC_ALL=C sort
    ;;
  distinct)
    # Count distinct values per group with a compound (SUBSEP) key.
    printf 'u1 a\nu1 a\nu1 b\nu2 a\n' \
      | run_awk '!seen[$1, $2]++ { n[$1]++ } END { for (u in n) print u, n[u] }' | LC_ALL=C sort
    ;;
  group)
    # Concatenate values per key and keep first-seen key order in a second, integer-indexed array.
    printf 'x 1\ny 2\nx 3\nz 4\ny 5\n' | run_awk '
      !($1 in g) { order[++n] = $1; g[$1] = $2; next }
      { g[$1] = g[$1] "," $2 }
      END { for (i = 1; i <= n; i++) print order[i], g[order[i]] }'
    ;;
  transpose)
    # Store a matrix under m[row, col], then print it column-major.
    printf '1 2 3\n4 5 6\n' | run_awk '
      { for (i = 1; i <= NF; i++) m[NR, i] = $i; if (NF > w) w = NF }
      END {
        for (i = 1; i <= w; i++) {
          line = m[1, i]
          for (r = 2; r <= NR; r++) line = line " " m[r, i]
          print line
        }
      }'
    ;;
  join)
    # Inner join: FILE1 is "id name", FILE2 is "order id"; print each order with its name.
    run_awk 'NR == FNR { name[$1] = $2; next } $2 in name { print $0, name[$2] }' "$@"
    ;;
  antijoin-naive)
    # Orders whose id is not in FILE1, with the NR == FNR test that breaks on an empty FILE1.
    run_awk 'NR == FNR { known[$1]; next } !($2 in known)' "$@"
    ;;
  antijoin-robust)
    # The same anti-join keyed on the file name, which an empty FILE1 cannot confuse.
    run_awk 'FILENAME == ARGV[1] { known[$1]; next } !($2 in known)' "$@"
    ;;
  reverse-bug)
    # l[lines] with lines still uninitialized stores line 1 under the subscript "", not "0".
    printf 'l1\nl2\nl3\n' | run_awk '
      { l[lines] = $0; ++lines }
      END { for (i = lines - 1; i >= 0; i--) print "[" l[i] "]" }'
    ;;
  reverse-fixed)
    # lines++ yields numeric 0 first, so the first subscript is "0".
    printf 'l1\nl2\nl3\n' | run_awk '
      { l[lines++] = $0 }
      END { for (i = lines - 1; i >= 0; i--) print "[" l[i] "]" }'
    ;;
  max-bug)
    # Comparing against a missing element compares against 0, so all-negative data never wins.
    printf 'c -5\nc -7\n' | run_awk '{ if ($2 > m[$1]) m[$1] = $2 } END { print "c=" m["c"] }'
    ;;
  max-fixed)
    printf 'c -5\nc -7\n' | run_awk '!($1 in m) || $2 > m[$1] { m[$1] = $2 } END { print "c=" m["c"] }'
    ;;
  keys)
    # Integer values convert with "%d"; others through CONVFMT ("%.6g"); string subscripts stay as written.
    # 2^53 is written as a constant: Debian's BusyBox awk is built without "^" ("Math support is not compiled in").
    run_awk 'BEGIN { a[01]; a["01"]; a[0.1 + 0.2]; a[1 / 3]; a[9007199254740992]; for (k in a) print k }' | LC_ALL=C sort
    ;;
  bigkey)
    # An integral value beyond the C integer range: the spec says "%d", implementations disagree.
    run_awk 'BEGIN { a[1e30]; for (k in a) print k }'
    ;;
  convfmt)
    # Changing CONVFMT changes the subscript a non-integer value maps to (BusyBox keeps x's cached string).
    run_awk 'BEGIN {
      x = 12.153; d[x] = 1; CONVFMT = "%2.2f"
      print ((x in d) ? "found" : "missing"), length(d)
      for (k in d) print "stored=" k
    }'
    ;;
  autoviv)
    # A reference creates the element; the in operator does not.
    run_awk 'BEGIN {
      if (a["x"] == "") r = "empty"
      if ("y" in b) r = r " y"
      print r, length(a), length(b)
    }'
    ;;
  subsep)
    # (i, j) joins with SUBSEP, so a key that already contains SUBSEP collides.
    run_awk 'BEGIN {
      a["1" SUBSEP "2", "3"]; a["1", "2" SUBSEP "3"]
      m[1, 2] = "v"
      for (k in m) n = split(k, p, SUBSEP)
      print length(a), ((1, 2) in m), n, p[1], p[2], (SUBSEP == "\034")
    }'
    ;;
  loop-add)
    # The loopcheck program from the gawk manual: add elements inside for-in, count iterations.
    run_awk 'BEGIN {
      a["here"]; a["is"]; a["a"]; a["loop"]
      for (i in a) { j++; a[j] = j; n++ }
      print "visited=" n, "size=" length(a)
    }'
    ;;
  loop-delete)
    # Delete every other element during the first iteration; count iterations and live keys seen.
    run_awk 'BEGIN {
      a["x"]; a["y"]; a["z"]
      for (k in a) {
        n++
        if (k in a) live++
        if (n == 1) for (o in a) if (o != k) delete a[o]
      }
      print "visited=" n, "live=" live, "size=" length(a)
    }'
    ;;
  clear)
    # Three ways to empty an array: delete arr (POSIX.1-2024), split("", arr), and element-wise delete.
    run_awk 'BEGIN {
      split("a b c", s); delete s; r = length(s)
      split("a b c", s); n = split("", s); r = r " " n " " length(s)
      split("a b c", s); for (k in s) delete s[k]; print r, length(s)
    }'
    ;;
  byref)
    # Arrays pass by reference; an untyped argument becomes an array; extra parameters are fresh locals.
    run_awk '
      function fill(arr) { arr["k"] = 1 }
      function count(x,    tmp) { tmp[x]; tmp[x "+"]; return length(tmp) }
      BEGIN { fill(t); print length(t), t["k"], count(1), count(2) }'
    ;;
  nulkey)
    # A record holding a NUL byte used as a subscript: length of each stored key.
    printf 'a\000b\n' | run_awk '{ k[$0] } END { for (x in k) print length(x) }'
    ;;
  sorted-in)
    # gawk orders for-in by PROCINFO["sorted_in"]; elsewhere PROCINFO is an ordinary array.
    run_awk 'BEGIN {
      a[10]; a[9]; a[1]; PROCINFO["sorted_in"] = "@ind_num_asc"
      for (k in a) line = line " " k
      print substr(line, 2)
    }'
    ;;
  *)
    echo "idioms.sh: unknown mode: $mode" >&2
    exit 2
    ;;
esac
