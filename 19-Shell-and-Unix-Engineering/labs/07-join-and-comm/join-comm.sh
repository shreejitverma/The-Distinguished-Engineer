#!/bin/sh
# join-comm.sh: probes for join(1) and comm(1), the two POSIX merge utilities.
# Usage: join-comm.sh MODE DIR
#   MODE is one of: demo cross sortkey blanks multiset disorder crlf ext-join ext-comm
#   DIR is an existing scratch directory; the probe writes its input files there and removes nothing.
# Every mode runs under LC_ALL=C so that sort, join, and comm agree on byte order.
# Tabs in comm output are printed as '|' so the column structure is visible.
set -u
LC_ALL=C
export LC_ALL

mode=${1:?usage: join-comm.sh MODE DIR}
dir=${2:?usage: join-comm.sh MODE DIR}
cd "$dir" || exit 2

# show_tabs: copy stdin to stdout with every tab replaced by '|'.
show_tabs() {
  tr '\t' '|'
}

# run_status NAME CMD...: run CMD with stdout to NAME.out and stderr to NAME.err,
# then print the stdout, the exit status, and whether anything reached stderr.
run_status() {
  name=$1
  shift
  "$@" >"$name.out" 2>"$name.err"
  st=$?
  show_tabs <"$name.out"
  if [ -s "$name.err" ]; then warned=yes; else warned=no; fi
  printf '%s: status=%d stderr=%s\n' "$name" "$st" "$warned"
}

case $mode in
  demo)
    # A customer table and an order table, keyed on the customer id.
    printf '%s\n' 'c3,Carol' 'c1,Alice' 'c2,Bob' 'c4,Dan' >users.raw
    printf '%s\n' 'c2,o-17' 'c1,o-11' 'c3,o-30' 'c1,o-12' 'c9,o-99' >orders.raw
    # Sort on the key field only: -k1,1 stops the key at the end of field 1.
    sort -t, -k1,1 users.raw >users
    sort -t, -k1,1 orders.raw >orders
    echo '--- inner join'
    join -t, users orders
    echo '--- left outer join, -o 0 keeps the key of unpaired lines'
    join -t, -a 1 -e NONE -o 0,1.2,2.2 users orders
    echo '--- full outer join'
    join -t, -a 1 -a 2 -e NONE -o 0,1.2,2.2 users orders
    echo '--- anti-join: users with no order'
    join -t, -v 1 users orders
    # comm works on whole lines, so project the key first.
    cut -d, -f1 users | sort -u >ids.users
    cut -d, -f1 orders | sort -u >ids.orders
    echo '--- comm -12: ids in both'
    comm -12 ids.users ids.orders
    echo '--- comm -3: ids in exactly one file (file 2 indented)'
    comm -3 ids.users ids.orders | show_tabs
    ;;
  cross)
    # Three lines with key k in file 1 and four in file 2 give 3 x 4 output lines.
    printf '%s\n' 'k a1' 'k a2' 'k a3' >f1
    printf '%s\n' 'k b1' 'k b2' 'k b3' 'k b4' >f2
    join f1 f2 >out
    wc -l <out | tr -d ' '
    sed -n '1p;$p' out
    ;;
  sortkey)
    # Keys 1, 10, 2 with ':' as the separator; ':' (0x3a) sorts after the digits (0x30-0x39).
    printf '%s\n' '2:z' '1:x' '10:y' >a.raw
    printf '%s\n' '2:r' '10:q' '1:p' >b.raw
    sort a.raw >a.line          # whole-line order: 10:y before 1:x
    sort -t: -k1,1 a.raw >a.key # key order: 1:x before 10:y
    sort -t: -k1,1 b.raw >b.key
    by_line=$(tr '\n' ' ' <a.line)
    by_key=$(tr '\n' ' ' <a.key)
    printf 'line-sorted: %s\n' "$by_line"
    printf 'key-sorted:  %s\n' "$by_key"
    run_status mixed join -t: a.line b.key
    run_status keyed join -t: a.key b.key
    ;;
  blanks)
    # Default fields: leading blanks ignored, runs of blanks are one separator, output uses one space.
    tab=$(printf '\t')
    printf '   k    1\n' >f1
    printf 'k%s%s2\n' "$tab" "$tab" >f2
    join f1 f2
    # With -t every separator counts, so the doubled tab yields an empty field 2.
    join -t "$tab" -o 0,2.2,2.3 -e EMPTY f2 f2 | show_tabs
    # An empty line has an empty join field, and empty join fields compare equal.
    printf '\nm 1\n' >g1
    printf '\nm 2\n' >g2
    join g1 g2 | sed 's/^$/<empty>/'
    ;;
  multiset)
    # comm compares occurrences, not sets: two bananas against one leaves one banana in column 1.
    printf '%s\n' apple banana banana cherry >c1
    printf '%s\n' banana cherry cherry date >c2
    comm c1 c2 | show_tabs
    echo '--- -23 (only in file 1)'
    comm -23 c1 c2
    ;;
  disorder)
    # File 1 is not sorted: b comes before a.
    printf '%s\n' b a c >d1
    printf '%s\n' a c >d2
    comm d1 d2 >d.out 2>d.err
    st=$?
    show_tabs <d.out
    if [ -s d.err ]; then warned=yes; else warned=no; fi
    printf 'comm: status=%d stderr=%s\n' "$st" "$warned"
    ;;
  crlf)
    # A carriage return is part of the line, so "id1<CR>" and "id1" are different lines.
    printf 'id1\r\nid2\r\n' >dos
    printf 'id1\nid2\n' >unix
    comm -12 dos unix >raw.common
    tr -d '\r' <dos >dos.fixed
    comm -12 dos.fixed unix >fixed.common
    raw=$(awk 'END { print NR }' raw.common)
    fixed=$(awk 'END { print NR }' fixed.common)
    printf 'common-raw=%d\ncommon-fixed=%d\n' "$raw" "$fixed"
    ;;
  ext-join)
    # join behavior outside POSIX, where implementations differ.
    printf '%s\n' 'a 1' 'b 2' >e1
    printf '%s\n' 'b 3' 'c 4' >e2
    run_status av join -a 1 -v 2 e1 e2
    printf '%s\n' 'K,,x' >t1
    printf '%s\n' 'K,y' >t2
    run_status fill join -t, -e NA t1 t2
    printf 'A 1\n' >i1
    printf 'a 2\n' >i2
    run_status joini join -i i1 i2
    ;;
  ext-comm)
    # comm behavior outside POSIX, where implementations differ.
    printf '%s\n' 'a 1' 'b 2' >e1
    printf '%s\n' 'b 3' 'c 4' >e2
    printf 'A\n' >j1
    printf 'a\n' >j2
    run_status commi comm -i j1 j2
    run_status total comm --total e1 e2
    ;;
  *)
    echo "join-comm.sh: unknown mode: $mode" >&2
    exit 2
    ;;
esac
