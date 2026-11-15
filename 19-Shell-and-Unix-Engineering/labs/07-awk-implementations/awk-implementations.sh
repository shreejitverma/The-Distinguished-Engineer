#!/bin/sh
# shellcheck disable=SC2016 # every single-quoted program here is awk code, expanded by awk
# Probe the behaviors that differ between awk implementations (gawk, mawk,
# the one true awk, BusyBox awk). Every probe runs the awk command given
# after the probe name, so one script compares any implementation.
#
# usage: awk-implementations.sh PROBE AWK [AWK-ARG...]
#        awk-implementations.sh sandbox OPTION AWK [AWK-ARG...]
# PROBE is one of: all seed system close syntax substr length inf div0 csv
# submatch trace bignum. "all" runs every probe except sandbox. For sandbox,
# OPTION is the implementation's sandbox flag (--sandbox, -safe) or "-" for none.
set -eu

# capture CMD...: run CMD, keep stdout (stderr dropped) in OUT and the exit status in ST.
capture() {
  if OUT=$("$@" 2>/dev/null); then ST=0; else ST=$?; fi
}

probe_seed() {
  # rand() with no srand(): POSIX leaves the initial seed unspecified.
  first=$("$@" 'BEGIN { print rand() }')
  second=$("$@" 'BEGIN { print rand() }')
  if [ "$first" = "$second" ]; then default=same; else default=differs; fi
  first=$("$@" 'BEGIN { srand(1); print rand() }')
  second=$("$@" 'BEGIN { srand(1); print rand() }')
  if [ "$first" = "$second" ]; then seeded=same; else seeded=differs; fi
  printf 'seed: default=%s seeded=%s\n' "$default" "$seeded"
}

probe_system() {
  # The second command makes the shell that system() starts kill itself with SIGTERM (15).
  r=$("$@" 'BEGIN { a = system("exit 3"); b = system("kill -TERM $$"); print a, b }')
  printf 'system: %s\n' "$r"
}

probe_close() {
  # close() of an output pipe whose command exits 3, then of an input pipe that exits 5.
  r=$("$@" 'BEGIN {
    c1 = "cat >/dev/null; exit 3"; print "x" | c1; r1 = close(c1)
    c2 = "exit 5"; c2 | getline line; r2 = close(c2)
    print r1, r2
  }')
  printf 'close: %s\n' "$r"
}

probe_syntax() {
  # Two extensions outside POSIX: the ** operator and the func keyword.
  capture "$@" 'BEGIN { print 2 ** 3 }'
  pow="$OUT/$ST"
  capture "$@" 'func f(x) { return x + 1 } BEGIN { print f(1) }'
  printf 'syntax: pow=%s func=%s\n' "$pow" "$OUT/$ST"
}

probe_substr() {
  # A start position below 1: POSIX does not say how the length is adjusted.
  r=$("$@" 'BEGIN { print substr("hello", 0, 2), substr("hello", -1, 3) }')
  printf 'substr: %s\n' "$r"
}

probe_length() {
  # length() of U+00E9, two bytes in UTF-8, under a UTF-8 locale and under C.
  utf8=$(LC_ALL=C.UTF-8 "$@" 'BEGIN { print length("\303\251") }')
  c=$(LC_ALL=C "$@" 'BEGIN { print length("\303\251") }')
  printf 'length: utf8=%s c=%s\n' "$utf8" "$c"
}

probe_inf() {
  # Overflow to infinity without a division: print uses OFMT, concatenation uses CONVFMT.
  r=$("$@" 'BEGIN { x = 1e308 * 10; print x, -x, (x ""), (-x "") }')
  printf 'inf: %s\n' "$r"
}

probe_div0() {
  capture "$@" 'BEGIN { z = 0; x = 1 / z; print (x > 1e308 ? "inf" : "finite") }'
  printf 'div0: out=%s status=%s\n' "$OUT" "$ST"
}

csv_run() {
  printf '%s\n' 'a,"b,c",d' | "$@" --csv '{ printf "NF=%d 2=<%s>\n", NF, $2 }'
}

probe_csv() {
  # --csv: a quoted field may contain the separator.
  capture csv_run "$@"
  printf 'csv: %s status=%s\n' "$OUT" "$ST"
}

probe_submatch() {
  # Three-argument match() is a gawk extension; it exposes the matcher's subexpression choice.
  prog='BEGIN { match("xyz", /(x|xy)(z|yz)/, m); printf "[%s][%s]", m[1], m[2] }'
  capture "$@" "$prog"
  default="$OUT"
  dst=$ST
  capture env GAWK_GNU_MATCHERS=1 "$@" "$prog"
  printf 'submatch: default=%s gnu=%s status=%s\n' "$default" "$OUT" "$dst"
}

probe_trace() {
  # gawk --trace prints each byte code it executes on stderr.
  out=$(printf 'a 2\n' | "$@" --trace '{ s += $2 } END { print s }' 2>&1) || true
  case $out in
    *"+ Op_field_spec"*"+ Op_assign_plus"*) ops=yes ;;
    *) ops=no ;;
  esac
  printf 'trace: ops=%s\n' "$ops"
}

probe_bignum() {
  # 2^64 + 1 needs 65 bits; a double has a 53-bit significand.
  capture "$@" -M 'BEGIN { print 2^64 + 1 }'
  printf 'bignum: %s\n' "$OUT"
}

probe_sandbox() {
  option=$1
  shift
  if [ "$option" = - ]; then
    capture "$@" 'BEGIN { system("echo ran") }'
  else
    capture "$@" "$option" 'BEGIN { system("echo ran") }'
  fi
  printf 'sandbox: out=%s status=%s\n' "$OUT" "$ST"
}

[ "$#" -ge 2 ] || {
  echo "usage: $0 PROBE AWK [AWK-ARG...]" >&2
  exit 64
}
probe=$1
shift
case $probe in
  all)
    for p in seed system close syntax substr length inf div0 csv submatch trace bignum; do
      "probe_$p" "$@"
    done
    ;;
  seed | system | close | syntax | substr | length | inf | div0 | csv | submatch | trace | bignum | sandbox)
    "probe_$probe" "$@"
    ;;
  *)
    echo "unknown probe: $probe" >&2
    exit 64
    ;;
esac
