#!/usr/bin/env bats
# POSIX leftmost-longest matching: which match regexec, sed, awk, and expr select,
# and how the C libraries split a tied match among subexpressions.
# The shell under test never chooses the regex engine: the C library (regoff, sed -E on
# glibc and musl, BusyBox) or the utility's own engine (gawk, mawk, the one true awk) does,
# so the regex tests branch on the library or the utility, never on lab_shell_family.

setup() {
  load ../lib/lab
  LAB_DIR="$BATS_TEST_DIRNAME"
  REGOFF="$BATS_TEST_TMPDIR/regoff"
  "${CC:-cc}" -Wall -Wextra -Werror -o "$REGOFF" "$LAB_DIR/regoff.c"
}

# lab_libc: the C library regoff was built against: darwin, glibc, musl, or other.
lab_libc() {
  "$REGOFF" -L
}

# lab_sed_flavor: gnu, bsd, busybox, or unknown, detected from the sed on PATH.
lab_sed_flavor() {
  local out
  out=$(sed --help 2>&1 </dev/null) || true
  case $out in
    *BusyBox*)
      echo busybox
      return
      ;;
    *) ;;
  esac
  out=$(sed --version 2>&1 </dev/null) || true
  case $out in
    *"GNU sed"*) echo gnu ;;
    *"illegal option"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# regoff ARG...: run the probe through the shell under test.
regoff() {
  # shellcheck disable=SC2016 # $0 and $@ belong to the shell under test
  run_lab_c '"$0" "$@"' "$REGOFF" "$@"
}

@test "regoff reports a C library family it was written for" {
  regoff -L
  [ "$status" -eq 0 ]
  case $output in
    darwin | glibc | musl) ;;
    *) false ;;
  esac
}

@test "leftmost beats longest: an empty match at offset 0 wins over aaa at offset 1" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'printf "%s\n" "$1" | sed "$2"' sh baaac 's/a*/X/'
  [ "$status" -eq 0 ]
  [ "$output" = Xbaaac ]
  regoff 'a*' baaac
  [ "$status" -eq 0 ]
  [ "$output" = "(0,0)" ]
}

@test "expr anchors at the first character, so a leading mismatch gives a zero-length match" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'expr "$1" : "$2"' sh aaab 'a*'
  [ "$status" -eq 0 ]
  [ "$output" = 3 ]
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'expr "$1" : "$2"' sh baaa 'a*'
  [ "$status" -eq 1 ]
  [ "$output" = 0 ]
}

@test "at the leftmost position the longest alternative wins, whatever its order" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'printf "%s\n" "$1" | sed -E "$2"' sh xabcd 's/a|ab|abc/X/'
  [ "$status" -eq 0 ]
  [ "$output" = xXd ]
  regoff -E 'a|ab|abc' xabcd
  [ "$status" -eq 0 ]
  [ "$output" = "(1,4)" ]
}

@test "awk match() sets RSTART and RLENGTH to the leftmost-longest match in every awk" {
  local awk_cmd awks words
  awks=$(lab_awks)
  [ -n "$awks" ]
  while IFS= read -r awk_cmd; do
    IFS=' ' read -r -a words <<<"$awk_cmd"
    run_lab "$LAB_DIR/leftmost.sh" xabcd 'a|ab|abc' "${words[@]}"
    [ "$status" -eq 0 ] || {
      echo "$awk_cmd: status $status"
      false
    }
    [ "$output" = "2 3 abc" ] || {
      echo "$awk_cmd: $output"
      false
    }
  done <<<"$awks"
}

@test "leftmost.sh reports a failed match as RSTART 0 and RLENGTH -1" {
  run_lab "$LAB_DIR/leftmost.sh" xyz 'a|ab'
  [ "$status" -eq 0 ]
  [ "$output" = "0 -1" ]
}

@test "the standard's example: (wee|week)(knights|night) takes all ten characters" {
  regoff -E '(wee|week)(knights|night)' weeknights
  [ "$status" -eq 0 ]
  [ "$output" = "(0,10)(0,3)(3,10)" ]
}

@test "the rationale's examples: the whole match decides first, then each subexpression" {
  regoff '\(ac*\)c*d[ac]*\1' acdacaaa
  [ "$status" -eq 0 ]
  [ "$output" = "(0,8)(0,1)" ]
  regoff -E '(a.*b)(a.*b)' accbaccccb
  [ "$status" -eq 0 ]
  [ "$output" = "(0,10)(0,4)(4,10)" ]
}

@test "a null string beats no match: \\(a*\\)* against bc reports an empty \\1" {
  regoff '\(a*\)*' bc
  [ "$status" -eq 0 ]
  [ "$output" = "(0,0)(0,0)" ]
}

@test "a two-way tie: POSIX gives the first subexpression the longer string" {
  regoff -E '(.|..)(..|.)' abc
  [ "$status" -eq 0 ]
  case $(lab_libc) in
    darwin) [ "$output" = "(0,3)(0,2)(2,3)" ] ;;
    glibc) [ "$output" = "(0,3)(0,1)(1,3)" ] ;; # first viable alternative, not the longest
    musl) skip "musl result not measured" ;;
    *) false ;;
  esac
}

@test "a group inside the last iteration that did not take part reports -1" {
  regoff -E '((a)|b)*' ab
  [ "$status" -eq 0 ]
  case $(lab_libc) in
    darwin) [ "$output" = "(0,2)(1,2)(-1,-1)" ] ;;
    glibc) [ "$output" = "(0,2)(1,2)(0,1)" ] ;; # stale capture from the first iteration
    musl) skip "musl result not measured" ;;
    *) false ;;
  esac
}

@test "a three-way split that the standard leaves ambiguous splits differently by library" {
  regoff -E '(a|ab)(c|bcd)(d*)' abcd
  [ "$status" -eq 0 ]
  case $(lab_libc) in
    darwin) [ "$output" = "(0,4)(0,2)(2,3)(3,4)" ] ;;
    glibc) [ "$output" = "(0,4)(0,1)(1,4)(4,4)" ] ;;
    musl) skip "musl result not measured" ;;
    *) false ;;
  esac
}

@test "the POSIX.1-2024 minimal modifier .*? is rejected or silently greedy in the C libraries" {
  regoff -E '.*?c' 'abc abc'
  case $(lab_libc) in
    darwin)
      [ "$status" -eq 2 ]
      [ "$output" = "regcomp: REG_BADRPT" ]
      ;;
    glibc | musl)
      [ "$status" -eq 0 ]
      [ "$output" = "(0,7)" ] # parsed as (.*)? : greedy, the whole string
      ;;
    *) false ;;
  esac
}

@test "macOS honors .*? only with REG_ENHANCED; other libraries have no such flag" {
  regoff -E -X '.*?c' 'abc abc'
  case $(lab_libc) in
    darwin)
      [ "$status" -eq 0 ]
      [ "$output" = "(0,3)" ]
      ;;
    glibc | musl)
      [ "$status" -eq 3 ]
      [ "$output" = "REG_ENHANCED undefined" ]
      ;;
    *) false ;;
  esac
}

@test "REG_MINIMAL: undefined in glibc and musl, defined but inert without REG_ENHANCED on macOS" {
  regoff -E -M '(.*)c' abcabc
  case $(lab_libc) in
    darwin)
      [ "$status" -eq 0 ]
      [ "$output" = "(0,6)(0,5)" ]
      regoff -E -M -X '(.*)c' abcabc
      [ "$status" -eq 0 ]
      [ "$output" = "(0,3)(0,2)" ]
      ;;
    glibc | musl)
      [ "$status" -eq 3 ]
      [ "$output" = "REG_MINIMAL undefined" ]
      ;;
    *) false ;;
  esac
}

@test "s///g skips an empty match that directly follows a non-empty one" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'printf "%s\n" "$1" | sed "$2"' sh baaac 's/a*/X/g'
  [ "$status" -eq 0 ]
  case $(lab_sed_flavor) in
    gnu | bsd | busybox) [ "$output" = XbXcX ] ;;
    *) false ;;
  esac
}

@test "awk gsub() with a pattern that can match empty makes three replacements in baaac" {
  local awk_cmd awks words
  awks=$(lab_awks)
  [ -n "$awks" ]
  while IFS= read -r awk_cmd; do
    IFS=' ' read -r -a words <<<"$awk_cmd"
    # shellcheck disable=SC2016 # the awk program is not shell code
    run_lab_c '"$@" '\''BEGIN { s = "baaac"; n = gsub(/a*/, "X", s); print n, s }'\''' sh "${words[@]}"
    [ "$output" = "3 XbXcX" ] || {
      echo "$awk_cmd: $output"
      false
    }
  done <<<"$awks"
}

@test "shell patterns are anchored: # and ## pick the shortest or longest prefix, not a leftmost match" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'x=aXbXc; printf "%s|%s|%s|%s\n" "${x#*X}" "${x##*X}" "${x%X*}" "${x%%X*}"'
  [ "$status" -eq 0 ]
  [ "$output" = "bXc|c|aXb|a" ]
}
