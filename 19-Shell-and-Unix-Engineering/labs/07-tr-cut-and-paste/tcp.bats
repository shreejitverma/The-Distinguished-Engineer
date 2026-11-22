#!/usr/bin/env bats
# Tests for tcp-probe.sh: tr padding, ranges, classes, and bytes versus characters;
# cut lists, delimiters, multibyte modes, encoding errors, and missing newlines;
# paste layouts, edge cases, encoding errors, descriptor limits, and long lines.
# The shell under test never changes which tr, cut, or paste runs, so every
# implementation-specific assertion branches on the flavor the probe detects:
# gnu (GNU coreutils), bsd (macOS and FreeBSD), or busybox.
# GNU cut gained multibyte -c and -n in coreutils 9.11, so cut arms also check CUT_MB.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/tcp-probe.sh"
}

# probe MODE: run the probe in a fresh scratch directory under the shell under test.
probe() {
  local d
  d=$(mktemp -d "$BATS_TEST_TMPDIR/probe.XXXXXX")
  run_lab "$PROBE" "$1" "$d"
}

# detect_flavors: set TR, CUT, PASTE, and CUT_MB from the probe's flavor mode.
detect_flavors() {
  probe flavor
  [ "$status" -eq 0 ]
  TR=${lines[0]#tr=}
  CUT=${lines[1]#cut=}
  PASTE=${lines[2]#paste=}
  CUT_MB=${lines[3]#cut_mb=}
}

@test "the probe recognizes the tr, cut, and paste implementations" {
  detect_flavors
  for f in "$TR" "$CUT" "$PASTE"; do
    case $f in
      gnu | bsd | busybox) true ;;
      *) false ;;
    esac
  done
  case $CUT in
    gnu) [ "$CUT_MB" = yes ] || [ "$CUT_MB" = no ] ;;
    bsd | busybox) [ "$CUT_MB" = n/a ] ;;
    *) false ;;
  esac
}

@test "tr pads a short string2 with its last character, and [c*] and [c*n] fill it portably" {
  detect_flavors
  probe trpad
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "pad_short: out=<abbbbbbbbb\n> rc=0 err=<>" ]
  [ "${lines[4]}" = "delete_nul: out=<ab\n> rc=0 err=<>" ]
  case $TR in
    gnu | bsd)
      [ "${lines[1]}" = "star_fill: out=<dddddddddd\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "star_count: out=<abbbcccccc\n> rc=0 err=<>" ]
      # A count with a leading zero is octal: [b*010] is eight b's.
      [ "${lines[3]}" = "star_octal: out=<bbbbbbbbcc\n> rc=0 err=<>" ]
      ;;
    busybox)
      # BusyBox has no [c*n]: the brackets, star, and digits are ordinary characters.
      [ "${lines[1]}" = "star_fill: out=<[d*]]]]]]]\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "star_count: out=<a[b*3]cccc\n> rc=0 err=<>" ]
      [ "${lines[3]}" = "star_octal: out=<[b*010]ccc\n> rc=0 err=<>" ]
      ;;
    *) false ;;
  esac
}

@test "a reversed tr range is an error in GNU, literal in BSD, and empty in BusyBox" {
  detect_flavors
  probe trrange
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "dash_first: out=<_Xb\n> rc=0 err=<>" ]
  [ "${lines[2]}" = "dash_last: out=<X_b\n> rc=0 err=<>" ]
  # System V brackets still work because [ and ] translate to themselves.
  [ "${lines[3]}" = "sysv_brackets: out=<[AB]\n> rc=0 err=<>" ]
  case $TR in
    gnu) [ "${lines[0]}" = "reversed: out=<> rc=1 err=<tr: range-endpoints of 'z-a' are in reverse collating sequence order>" ] ;;
    bsd) [ "${lines[0]}" = "reversed: out=<XXmX\n> rc=0 err=<>" ] ;;
    busybox) [ "${lines[0]}" = "reversed: out=<azm-\n> rc=0 err=<>" ] ;;
    *) false ;;
  esac
}

@test "tr class rules in string2 and missing classes differ by implementation" {
  detect_flavors
  probe trclass
  [ "$status" -eq 0 ]
  # Padding a single character works everywhere, unlike the POSIX [\n*] form on BusyBox.
  [ "${lines[1]}" = "words_short: out=<hello\nworld\n> rc=0 err=<>" ]
  case $TR in
    gnu)
      [ "${lines[0]}" = "words_posix: out=<hello\nworld\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "digit_in_s2: out=<> rc=1 err=<tr: when translating, the only character classes that may appear in>" ]
      [ "${lines[3]}" = "delete_graph: out=< \n> rc=0 err=<>" ]
      ;;
    bsd)
      [ "${lines[0]}" = "words_posix: out=<hello\nworld\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "digit_in_s2: out=<012\n> rc=0 err=<>" ]
      [ "${lines[3]}" = "delete_graph: out=< \n> rc=0 err=<>" ]
      ;;
    busybox)
      # No [c*]: the complement maps to [, newline, *, then ] for every later byte.
      [ "${lines[0]}" = "words_posix: out=<hello]world]> rc=0 err=<>" ]
      [ "${lines[2]}" = "digit_in_s2: out=<012\n> rc=0 err=<>" ]
      # No [:graph:]: the eight distinct characters of the operand are deleted one by one.
      [ "${lines[3]}" = "delete_graph: out=< b\n> rc=0 err=<>" ]
      ;;
    *) false ;;
  esac
}

@test "BSD tr decodes characters and dies on an invalid byte; GNU and BusyBox tr map bytes" {
  detect_flavors
  probe trmb
  [ "$status" -eq 0 ]
  [ "${lines[3]}" = "invalid_c: out=<x\377b\n> rc=0 err=<>" ]
  case $TR in
    bsd)
      [ "${lines[0]}" = "upper_utf8: out=<\303\211A\n> rc=0 err=<>" ]
      # Each octal escape is one wide character (U+00C3, U+00A9), so the UTF-8 e-acute never matches.
      [ "${lines[1]}" = "octal_pair: out=<a\303\251b\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "invalid_utf8: out=<x> rc=1 err=<tr: Illegal byte sequence>" ]
      ;;
    gnu | busybox)
      [ "${lines[0]}" = "upper_utf8: out=<\303\251A\n> rc=0 err=<>" ]
      [ "${lines[1]}" = "octal_pair: out=<axyb\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "invalid_utf8: out=<x\377b\n> rc=0 err=<>" ]
      ;;
    *) false ;;
  esac
}

@test "cut writes selections in input order once, passes undelimited lines, and rejects zero" {
  detect_flavors
  probe cutlist
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "order: out=<abdefg\n> rc=0 err=<>" ]
  [ "${lines[1]}" = "field_order: out=<a:c\n> rc=0 err=<>" ]
  [ "${lines[2]}" = "passthrough: out=<nodelim\nb\n> rc=0 err=<>" ]
  [ "${lines[3]}" = "suppress: out=<b\n> rc=0 err=<>" ]
  [ "${lines[4]}" = "beyond: out=<\n> rc=0 err=<>" ]
  case $CUT in
    gnu) [ "${lines[5]}" = "zero: out=<> rc=1 err=<cut: fields are numbered from 1>" ] ;;
    bsd) [ "${lines[5]}" = "zero: out=<> rc=1 err=<cut: [-bcf] list: values may not include zero>" ] ;;
    busybox) [ "${lines[5]}" = "zero: out=<a\n> rc=0 err=<>" ] ;; # 0 and 1 both map to the first field
    *) false ;;
  esac
}

@test "cut -d accepts exactly one character, and an empty -d means NUL only in GNU" {
  detect_flavors
  probe cutdelim
  [ "$status" -eq 0 ]
  # Adjacent delimiters delimit an empty field: cut never merges runs of spaces.
  [ "${lines[2]}" = "space_f2: out=<\n> rc=0 err=<>" ]
  [ "${lines[3]}" = "space_f3: out=<b\n> rc=0 err=<>" ]
  case $CUT in
    gnu)
      [ "${lines[0]}" = "empty_delim: out=<b\tc\n> rc=0 err=<>" ]
      [ "${lines[1]}" = "two_char_delim: out=<> rc=1 err=<cut: the delimiter must be a single character>" ]
      ;;
    bsd)
      [ "${lines[0]}" = "empty_delim: out=<> rc=1 err=<cut: bad delimiter>" ]
      [ "${lines[1]}" = "two_char_delim: out=<> rc=1 err=<cut: bad delimiter>" ]
      ;;
    busybox)
      # An empty -d falls back to tab, and the line reader stops at a NUL as at a
      # newline, so "a" and "b<TAB>c" are cut as two lines.
      [ "${lines[0]}" = "empty_delim: out=<a\nc\n> rc=0 err=<>" ]
      # Only the first byte of -d is used.
      [ "${lines[1]}" = "two_char_delim: out=<a;b\n> rc=0 err=<>" ]
      ;;
    *) false ;;
  esac
}

@test "cut -c and -n are character aware only in BSD and GNU 9.11 or later" {
  detect_flavors
  probe cutmb
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "b1: out=<\303\n> rc=0 err=<>" ]
  case $CUT-$CUT_MB in
    bsd-n/a | gnu-yes)
      [ "${lines[0]}" = "c1: out=<\303\251\n> rc=0 err=<>" ]
      # -n drops a character whose last byte is past high, and keeps one whose first byte is before low.
      [ "${lines[2]}" = "nb1: out=<\n> rc=0 err=<>" ]
      [ "${lines[3]}" = "nb2: out=<\303\251\n> rc=0 err=<>" ]
      ;;
    gnu-no | busybox-n/a)
      # -c is -b and -n is ignored.
      [ "${lines[0]}" = "c1: out=<\303\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "nb1: out=<\303\n> rc=0 err=<>" ]
      [ "${lines[3]}" = "nb2: out=<\251\n> rc=0 err=<>" ]
      ;;
    *) false ;;
  esac
}

@test "BSD cut -f stops at an invalid byte and exits 0 for a file operand" {
  detect_flavors
  probe cutbad
  [ "$status" -eq 0 ]
  [ "${lines[2]}" = "c_locale: out=<1\n2\n3\n> rc=0 err=<>" ]
  case $CUT in
    bsd)
      [ "${lines[0]}" = "file_operand: out=<1\n> rc=0 err=<cut: bad: Illegal byte sequence>" ]
      [ "${lines[1]}" = "stdin_input: out=<1\n> rc=1 err=<cut: stdin: Illegal byte sequence>" ]
      ;;
    gnu | busybox)
      [ "${lines[0]}" = "file_operand: out=<1\n2\n3\n> rc=0 err=<>" ]
      [ "${lines[1]}" = "stdin_input: out=<1\n2\n3\n> rc=0 err=<>" ]
      ;;
    *) false ;;
  esac
}

@test "a final line without a newline gets one, except BSD cut -c past the end" {
  detect_flavors
  probe cuteol
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "field_no_newline: out=<b\n> rc=0 err=<>" ]
  case $CUT in
    gnu | busybox) [ "${lines[0]}" = "past_end: out=<\n> rc=0 err=<>" ] ;;
    bsd) [ "${lines[0]}" = "past_end: out=<> rc=0 err=<>" ] ;;
    *) false ;;
  esac
}

@test "paste fills exhausted files with empty lines, except BusyBox, which skips them" {
  detect_flavors
  probe pastelayout
  [ "$status" -eq 0 ]
  [ "${lines[3]}" = "cycle: out=<1,1;1\n2,2;2\n3,3;3\n> rc=0 err=<>" ]
  [ "${lines[4]}" = "serial_cycle: out=<1,2;3\n> rc=0 err=<>" ]
  # \0 in the list is an empty delimiter, not a NUL byte.
  [ "${lines[5]}" = "serial_nodelim: out=<123\n> rc=0 err=<>" ]
  case $PASTE in
    gnu | bsd)
      [ "${lines[0]}" = "long_first: out=<1\tx\n2\t\n3\t\n> rc=0 err=<>" ]
      [ "${lines[1]}" = "long_last: out=<x\t1\n\t2\n\t3\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "stdin_twice: out=<a\tb\nc\td\ne\t\n> rc=0 err=<>" ]
      ;;
    busybox)
      [ "${lines[0]}" = "long_first: out=<1\tx\n2\t3\t> rc=0 err=<>" ]
      [ "${lines[1]}" = "long_last: out=<x\t1\n2\n3\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "stdin_twice: out=<a\tb\nc\td\ne\t> rc=0 err=<>" ]
      ;;
    *) false ;;
  esac
}

@test "paste -s on an empty or unterminated file, an empty -d, and no operands differ" {
  detect_flavors
  probe pasteedge
  [ "$status" -eq 0 ]
  case $PASTE in
    gnu)
      [ "${lines[0]}" = "serial_empty: out=<\n> rc=0 err=<>" ]
      [ "${lines[1]}" = "serial_noeol: out=<a\tb\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "empty_list: out=<xx\n> rc=0 err=<>" ]
      [ "${lines[3]}" = "no_operand: out=<1\n2\n3\n> rc=0 err=<>" ]
      ;;
    bsd)
      [ "${lines[0]}" = "serial_empty: out=<> rc=0 err=<>" ]
      [ "${lines[1]}" = "serial_noeol: out=<a\tb> rc=0 err=<>" ]
      [ "${lines[2]}" = "empty_list: out=<> rc=1 err=<paste: no delimiters specified>" ]
      [ "${lines[3]}" = "no_operand: out=<> rc=1 err=<usage: paste [-s] [-d delimiters] file ...>" ]
      ;;
    busybox)
      [ "${lines[0]}" = "serial_empty: out=<> rc=0 err=<>" ]
      [ "${lines[1]}" = "serial_noeol: out=<a\tb\n> rc=0 err=<>" ]
      [ "${lines[2]}" = "empty_list: out=<> rc=1 err=<paste: -d '' is not supported>" ]
      [ "${lines[3]}" = "no_operand: out=<1\n2\n3\n> rc=0 err=<>" ]
      ;;
    *) false ;;
  esac
}

@test "BSD paste stops at an invalid byte: status 74 in parallel, a silent 0 with -s" {
  detect_flavors
  probe pastebad
  [ "$status" -eq 0 ]
  case $PASTE in
    bsd)
      [ "${lines[0]}" = "parallel: out=<a> rc=74 err=<paste: Error reading bad>" ]
      [ "${lines[1]}" = "serial: out=<a> rc=0 err=<>" ]
      ;;
    gnu | busybox)
      [ "${lines[0]}" = "parallel: out=<a\377b\t1\nc\t2\n> rc=0 err=<>" ]
      [ "${lines[1]}" = "serial: out=<a\377b\tc\n> rc=0 err=<>" ]
      ;;
    *) false ;;
  esac
}

@test "parallel paste needs one descriptor per operand and writes nothing when one fails" {
  detect_flavors
  probe pastefd
  [ "$status" -eq 0 ]
  [ "${lines[2]}" = "subshell=0" ]
  case ${lines[0]} in
    "parallel: rc=0 "*) false ;;
    "parallel: rc="*" stdout_bytes=0") true ;;
    *) false ;;
  esac
  case $PASTE in
    gnu | bsd) [ "${lines[1]}" = "serial: rc=0 lines=40" ] ;;
    # BusyBox opens every operand before it looks at -s, so -s fails the same way.
    busybox) [ "${lines[1]}" = "serial: rc=1 lines=0" ] ;;
    *) false ;;
  esac
}

@test "cut, paste, and tr handle a 1 MiB line, far past LINE_MAX" {
  probe longline
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "field2: out=<z\n> rc=0 err=<>" ]
  [ "${lines[1]}" = "field1_bytes=1048577" ]
  [ "${lines[2]}" = "pasted_bytes=2097158" ]
  [ "${lines[3]}" = "tr_delete: out=<:z\n> rc=0 err=<>" ]
}
