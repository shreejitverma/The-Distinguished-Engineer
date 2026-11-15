#!/usr/bin/env bats
# Tests for awk-implementations.sh: each test runs one probe against every awk
# installed here (lab_awks) and asserts what that implementation really does.
# The shell under test never changes which awk runs, so every branch is on the
# awk implementation (lab_awk_flavor) and, where releases differ, its version.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME/awk-implementations.sh"
  AWKS=()
  local line list
  list=$(lab_awks)
  while IFS= read -r line; do
    [ -n "$line" ] && AWKS+=("$line")
  done <<<"$list"
  # No awk at all must fail, not pass vacuously.
  [ "${#AWKS[@]}" -gt 0 ]
}

# gawk_version AWK...: print a gawk release as major * 100 + minor (5.4.1 -> 504).
gawk_version() {
  local banner major minor
  banner=$("$@" --version 2>/dev/null </dev/null) || true
  banner=${banner%%$'\n'*}
  major=${banner#GNU Awk }
  major=${major%%.*}
  minor=${banner#GNU Awk *.}
  minor=${minor%%.*}
  printf '%s\n' "$((major * 100 + minor))"
}

# onetrue_date AWK...: print the release date of a one true awk ("awk version 20200816" -> 20200816).
onetrue_date() {
  local banner
  banner=$("$@" --version 2>&1 </dev/null) || true
  banner=${banner#*version }
  printf '%s\n' "${banner:0:8}"
}

@test "rand() without srand() repeats in gawk, the one true awk, BusyBox, and Debian 13 mawk 20250131; other mawk builds seed from the clock" {
  local a flavor banner
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" seed $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk | onetrue | busybox) [ "$output" = "seed: default=same seeded=same" ] ;;
      mawk)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        banner=$($a -W version 2>/dev/null </dev/null | head -n 1) || true
        if [ "${banner##* }" = 20250131 ]; then
          # Debian 13's build: the documented clock seed did not take effect (shell matrix).
          [ "$output" = "seed: default=same seeded=same" ]
        else
          [ "$output" = "seed: default=differs seeded=same" ] # mawk(1): seeded from the clock
        fi
        ;;
      *) false ;;
    esac
  done
}

@test "system() reports a signal death as 256 + signal, except BusyBox, which reports 0" {
  local a flavor
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" system $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk | mawk | onetrue) [ "$output" = "system: 3 271" ] ;;
      busybox) [ "$output" = "system: 3 0" ] ;; # system(3) status >> 8 drops the signal
      *) false ;;
    esac
  done
}

@test "close() of a pipe returns the exit status in gawk and mawk, 0 in the one true awk, the raw wait status in BusyBox" {
  local a flavor
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" close $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk | mawk) [ "$output" = "close: 3 5" ] ;;
      onetrue) [ "$output" = "close: 0 0" ] ;;
      busybox) [ "$output" = "close: 768 1280" ] ;; # pclose(3) value: exit status << 8
      *) false ;;
    esac
  done
}

@test "mawk rejects the ** operator and the func keyword; the others accept both, except that BusyBox built without math support rejects **" {
  local a flavor
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" syntax $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk | onetrue) [ "$output" = "syntax: pow=8/0 func=2/0" ] ;;
      # BusyBox without FEATURE_AWK_LIBM (Debian's build) rejects ** and ^ at run time, exit 1.
      busybox) [ "$output" = "syntax: pow=8/0 func=2/0" ] || [ "$output" = "syntax: pow=/1 func=2/0" ] ;;
      mawk) [[ $output =~ ^syntax:\ pow=/[1-9][0-9]*\ func=/[1-9][0-9]*$ ]] || false ;;
      *) false ;;
    esac
  done
}

@test "substr() with a start below 1: mawk widens the length, the others clamp the start to 1" {
  local a flavor
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" substr $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk | onetrue | busybox) [ "$output" = "substr: he hel" ] ;;
      mawk) [ "$output" = "substr: he hell" ] ;; # bi_substr: n -= i + 1 for a start of -1
      *) false ;;
    esac
  done
}

@test "length() counts characters only in gawk and the one true awk since 2023, and only under a UTF-8 locale" {
  local a flavor date
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" length $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk) [ "$output" = "length: utf8=1 c=2" ] ;;
      onetrue)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        date=$(onetrue_date $a)
        if [ "$date" -ge 20231030 ]; then
          [ "$output" = "length: utf8=1 c=2" ] # FIXES Oct 30, 2023: no UTF-8 in C or POSIX
        elif [ "$date" -ge 20230909 ]; then
          [ "$output" = "length: utf8=1 c=1" ] # second edition: UTF-8 in every locale
        else
          [ "$output" = "length: utf8=2 c=2" ] # before the second edition: bytes
        fi
        ;;
      mawk | busybox) [ "$output" = "length: utf8=2 c=2" ] ;;
      *) false ;;
    esac
  done
}

@test "infinity prints as +inf in gawk and the one true awk since 2020, in mawk only through print, never in BusyBox" {
  local a flavor date
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" inf $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk) [ "$output" = "inf: +inf -inf +inf -inf" ] ;;
      onetrue)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        date=$(onetrue_date $a)
        if [ "$date" -ge 20201218 ]; then
          [ "$output" = "inf: +inf -inf +inf -inf" ] # FIXES December 18, 2020
        else
          [ "$output" = "inf: inf -inf inf -inf" ]
        fi
        ;;
      mawk) [ "$output" = "inf: +inf -inf inf -inf" ] ;; # print.c adds the sign; CONVFMT does not
      busybox) [ "$output" = "inf: inf -inf inf -inf" ] ;;
      *) false ;;
    esac
  done
}

@test "division by zero is fatal everywhere except mawk, which yields IEEE infinity" {
  local a flavor
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" div0 $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk | onetrue) [ "$output" = "div0: out= status=2" ] ;;
      busybox) [ "$output" = "div0: out= status=1" ] ;; # bb_error_msg_and_die
      mawk) [ "$output" = "div0: out=inf status=0" ] ;;
      *) false ;;
    esac
  done
}

@test "--csv splits quoted fields only in gawk 5.3+ and the one true awk since the second edition" {
  local a flavor date ver
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" csv $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        ver=$(gawk_version $a)
        if [ "$ver" -ge 503 ]; then
          [ "$output" = "csv: NF=3 2=<b,c> status=0" ]
        else
          [ "$output" != "csv: NF=3 2=<b,c> status=0" ]
        fi
        ;;
      onetrue)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        date=$(onetrue_date $a)
        if [ "$date" -ge 20230909 ]; then
          [ "$output" = "csv: NF=3 2=<b,c> status=0" ]
        else
          [ "$output" != "csv: NF=3 2=<b,c> status=0" ]
        fi
        ;;
      mawk | busybox) [ "$output" != "csv: NF=3 2=<b,c> status=0" ] ;;
      *) false ;;
    esac
  done
}

@test "three-argument match(): gawk 5.4 MinRX picks POSIX subexpressions, BusyBox ignores the array, mawk and the one true awk reject it" {
  local a flavor ver
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" submatch $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        ver=$(gawk_version $a)
        if [ "$ver" -ge 504 ]; then
          [ "$output" = "submatch: default=[xy][z] gnu=[x][yz] status=0" ]
        else
          [ "$output" = "submatch: default=[x][yz] gnu=[x][yz] status=0" ]
        fi
        ;;
      busybox) [ "$output" = "submatch: default=[][] gnu=[][] status=0" ] ;;
      mawk | onetrue) [[ $output =~ ^submatch:\ default=\ gnu=\ status=[1-9][0-9]*$ ]] || false ;;
      *) false ;;
    esac
  done
}

@test "only gawk 5.1+ traces the byte codes it executes" {
  local a flavor ver
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" trace $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        ver=$(gawk_version $a)
        if [ "$ver" -ge 501 ]; then
          [ "$output" = "trace: ops=yes" ]
        else
          [ "$output" = "trace: ops=no" ]
        fi
        ;;
      mawk | onetrue | busybox) [ "$output" = "trace: ops=no" ] ;;
      *) false ;;
    esac
  done
}

@test "only gawk -M computes 2^64 + 1 exactly; a double rounds it" {
  local a flavor
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB" bignum $a
    [ "$status" -eq 0 ]
    case $flavor in
      gawk) [ "$output" = "bignum: 18446744073709551617" ] ;;
      mawk | onetrue | busybox) [ "$output" != "bignum: 18446744073709551617" ] ;;
      *) false ;;
    esac
  done
}

@test "gawk --sandbox and one true awk -safe refuse system(); mawk and BusyBox have no such mode and run it" {
  local a flavor
  for a in "${AWKS[@]}"; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $a)
    case $flavor in
      gawk)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        run_lab "$LAB" sandbox --sandbox $a
        [ "$output" = "sandbox: out= status=2" ]
        ;;
      onetrue)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        run_lab "$LAB" sandbox -safe $a
        [ "$output" = "sandbox: out= status=2" ]
        ;;
      mawk | busybox)
        # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
        run_lab "$LAB" sandbox - $a
        [ "$output" = "sandbox: out=ran status=0" ]
        ;;
      *) false ;;
    esac
    [ "$status" -eq 0 ]
  done
}

@test "the all probe runs every probe except sandbox, in order" {
  run_lab "$LAB" all awk
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 12 ]
  local i want
  i=0
  for want in seed system close syntax substr length inf div0 csv submatch trace bignum; do
    [ "${lines[$i]%%:*}" = "$want" ]
    i=$((i + 1))
  done
}

@test "an unknown probe or a missing awk command is a usage error" {
  run_lab "$LAB" nosuch awk
  [ "$status" -eq 64 ]
  [ "$output" = "unknown probe: nosuch" ]
  run_lab "$LAB" seed
  [ "$status" -eq 64 ]
}
