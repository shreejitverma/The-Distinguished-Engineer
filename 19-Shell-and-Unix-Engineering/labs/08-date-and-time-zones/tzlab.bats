#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031 # each @test exports its own TZ on purpose; tests never share it
# Tests for the date and Time Zones note.
# date(1) behavior depends on the implementation (GNU, BSD and macOS, BusyBox), and zone
# arithmetic on the C library, so those tests branch on tzlab.sh impl and libc; the shell
# under test matters only for arithmetic, export semantics, and the printf builtin.
# Every zone below is a POSIX TZ string, so no test needs the tz database installed.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME/tzlab.sh"
  IMPL=$("$LAB" impl)
  LIBC=$("$LAB" libc)
  # A US-style rule set under made-up names, so no TZif file can shadow the POSIX parse.
  NYRULE='NYS5NYD,M3.2.0,M11.1.0'
}

@test "date +%s equals the XBD 4.19 formula for the same instant" {
  run_lab "$LAB" now-check
  [ "$status" -eq 0 ]
  seconds=${output% *}
  formula=${output#* }
  case $seconds in
    *[!0-9]* | "") false ;;
    *) true ;;
  esac
  [ "$seconds" -gt 1700000000 ]
  [ "$seconds" = "$formula" ]
}

@test "the XBD 4.19 formula reaches 2^31 at 2038-01-19 03:14:08, except in 32-bit mksh" {
  run_lab "$LAB" posix 2038 18 3 14 8
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    mksh) [ "$output" = -2147483648 ] ;; # mksh arithmetic is exactly 32-bit with silent wraparound
    bash | zsh | zsh-sh | dash | ksh93 | yash | posh | busybox) [ "$output" = 2147483648 ] ;;
    *) false ;;
  esac
}

@test "pure-shell days_from_civil and civil_from_days agree with fixed instants" {
  run_lab "$LAB" epoch 2000 02 29 12 34 56
  [ "$output" = 951827696 ]
  run_lab "$LAB" epoch 2026 03 08 07 00 00
  [ "$output" = 1772953200 ]
  run_lab "$LAB" civil -1
  [ "$output" = "1969-12-31 23:59:59" ]
  run_lab "$LAB" civil -31536000
  [ "$output" = "1969-01-01 00:00:00" ]
  run_lab "$LAB" civil 951827696
  [ "$output" = "2000-02-29 12:34:56" ]
  run_lab "$LAB" civil 1772953200
  [ "$output" = "2026-03-08 07:00:00" ]
}

@test "date formats the same fixed instants identically in UTC" {
  export TZ=UTC0
  run_lab "$LAB" fmt 0 '%Y-%m-%d %H:%M:%S'
  [ "$status" -eq 0 ]
  [ "$output" = "1970-01-01 00:00:00" ]
  run_lab "$LAB" fmt 951827696 '%Y-%m-%d %H:%M:%S'
  [ "$output" = "2000-02-29 12:34:56" ]
  run_lab "$LAB" fmt 1772953200 '%Y-%m-%d %H:%M:%S %j'
  [ "$output" = "2026-03-08 07:00:00 067" ]
}

@test "instants after 2038 overflow mksh arithmetic but not 64-bit shells" {
  run_lab "$LAB" civil 4102444800
  case $(lab_shell_family) in
    mksh) [ "$output" != "2100-01-01 00:00:00" ] ;;
    bash | zsh | zsh-sh | dash | ksh93 | yash | posh | busybox) [ "$output" = "2100-01-01 00:00:00" ] ;;
    *) false ;;
  esac
}

@test "leading zeros from %j are stripped before arithmetic" {
  for pair in 000:0 008:8 009:9 010:10 365:365; do
    run_lab "$LAB" strip "${pair%%:*}"
    [ "$status" -eq 0 ]
    [ "$output" = "${pair#*:}" ]
  done
}

@test "a raw 009 in arithmetic is bad octal in POSIX-mode shells and decimal in others" {
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'j=009; echo $((j - 1))'
  case $(lab_shell_family) in
    bash)
      [ "$status" -ne 0 ] # 1, or 127 in POSIX mode (measured: bash 3.2 as sh, bash 5.3 --posix)
      [[ $output == *"value too great for base"* ]]
      ;;
    dash)
      [ "$status" -ne 0 ]
      [[ $output == *"Illegal number"* ]]
      ;;
    zsh-sh)
      [ "$status" -ne 0 ]
      [[ $output == *"bad math expression"* ]]
      ;;
    busybox | posh)
      [ "$status" -ne 0 ]
      [ "$output" != 8 ]
      ;;
    # yash 2.58.1 falls back to decimal for an invalid octal constant unless -o posix is set.
    zsh | ksh93 | mksh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = 8 ]
      ;;
    *) false ;;
  esac
}

@test "-u behaves as TZ=UTC0 whatever TZ says" {
  export TZ='<+0345>-3:45'
  run_lab_c 'date +%z; date -u +%z'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = +0345 ]
  [ "${lines[1]}" = +0000 ]
}

@test "POSIX TZ offsets are positive west of Greenwich" {
  export TZ='<-03>3'
  run_lab "$LAB" fmt 0 '%H:%M %z %Z'
  [ "$status" -eq 0 ]
  [ "$output" = "21:00 -0300 -03" ]
  export TZ=UTC+3
  run_lab "$LAB" fmt 0 '%H:%M %z %Z'
  [ "$output" = "21:00 -0300 UTC" ]
}

@test "an unknown zone name is silently UTC; only the abbreviation differs by libc" {
  export TZ=Nowhere/Bogus
  run_lab "$LAB" fmt 0 '%H:%M %z|%Z'
  [ "$status" -eq 0 ]
  case $LIBC in
    glibc) [ "$output" = "00:00 +0000|Nowhere" ] ;; # tzset parses the leading letters as std
    musl | darwin) [ "$output" = "00:00 +0000|UTC" ] ;;
    *) false ;;
  esac
}

@test "a rule-based TZ string switches offset at the rule's instant" {
  export TZ=$NYRULE
  run_lab "$LAB" fmt 1772953199 '%F %T %Z %z'
  [ "$status" -eq 0 ]
  [ "$output" = "2026-03-08 01:59:59 NYS -0500" ]
  run_lab "$LAB" fmt 1772953200 '%F %T %Z %z'
  [ "$output" = "2026-03-08 03:00:00 NYD -0400" ]
}

@test "a TZ assignment reaches date only once TZ is exported" {
  unset TZ
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'a=$(date +%z); TZ="<+0345>-3:45"; b=$(date +%z); export TZ; c=$(date +%z); echo "$a|$b|$c"'
  [ "$status" -eq 0 ]
  a=${output%%|*}
  [ "$output" = "$a|$a|+0345" ]
}

@test "an inherited TZ is already exported, so a plain assignment reaches date" {
  export TZ='<-05>5'
  run_lab_c 'TZ="<+0345>-3:45"; date +%z'
  [ "$status" -eq 0 ]
  [ "$output" = +0345 ]
}

@test "a local time in the spring-forward gap: GNU rejects, BSD moves forward, musl moves back" {
  export TZ=$NYRULE
  run_lab "$LAB" parse '2026-03-08 02:30:00' '%H:%M %Z'
  case $IMPL in
    gnu)
      [ "$status" -ne 0 ]
      [[ $output == *"invalid date"* ]]
      ;;
    bsd)
      [ "$status" -eq 0 ]
      [ "$output" = "03:30 NYD" ]
      ;;
    busybox)
      [ "$LIBC" = musl ] # the expected value below is derived from musl mktime()
      [ "$status" -eq 0 ]
      [ "$output" = "01:30 NYS" ]
      ;;
    *) false ;;
  esac
}

@test "adding a month to January 31: GNU overflows, BSD clamps, BusyBox has no arithmetic" {
  export TZ=UTC0
  run_lab "$LAB" add '2026-01-31 00:00:00' 1 month %F
  case $IMPL in
    gnu)
      [ "$status" -eq 0 ]
      [ "$output" = 2026-03-03 ]
      ;;
    bsd)
      [ "$status" -eq 0 ]
      [ "$output" = 2026-02-28 ]
      ;;
    busybox)
      [ "$status" -ne 0 ]
      [[ $output == *"invalid date"* ]]
      ;;
    *) false ;;
  esac
}

@test "adding a year to February 29 lands on March 1 in GNU and BSD" {
  export TZ=UTC0
  run_lab "$LAB" add '2024-02-29 00:00:00' 1 year %F
  case $IMPL in
    gnu | bsd)
      [ "$status" -eq 0 ]
      [ "$output" = 2025-03-01 ]
      ;;
    busybox) [ "$status" -ne 0 ] ;;
    *) false ;;
  esac
}

@test "across a DST start, 1 day keeps the wall clock and 24 hours keeps elapsed time" {
  export TZ=$NYRULE
  run_lab "$LAB" add '2026-03-07 12:00:00' 1 day '%F %H:%M %Z'
  case $IMPL in
    gnu | bsd)
      [ "$status" -eq 0 ]
      [ "$output" = "2026-03-08 12:00 NYD" ]
      run_lab "$LAB" add '2026-03-07 12:00:00' 24 hour '%F %H:%M %Z'
      [ "$status" -eq 0 ]
      [ "$output" = "2026-03-08 13:00 NYD" ]
      ;;
    busybox) [ "$status" -ne 0 ] ;;
    *) false ;;
  esac
}

@test "a signed number after a clock time is a UTC offset to GNU date" {
  export TZ=UTC0
  run_lab "$LAB" parse '2026-03-07 12:00:00 +1 day' '%F %T'
  case $IMPL in
    gnu)
      # 12:00 at +01 is 11:00 UTC; the "day" that follows is a bare relative unit.
      [ "$status" -eq 0 ]
      [ "$output" = "2026-03-08 11:00:00" ]
      ;;
    bsd)
      # strptime() stops after the format and date(1) warns about the rest.
      [ "$status" -eq 0 ]
      [[ $output == *"Ignoring 7 extraneous characters"* ]]
      [ "${lines[1]}" = "2026-03-07 12:00:00" ]
      ;;
    busybox)
      [ "$status" -ne 0 ]
      [[ $output == *"invalid date"* ]]
      ;;
    *) false ;;
  esac
}

@test "printf %(fmt)T formats time without date(1) only in bash 4.2+ and ksh93" {
  export TZ=UTC0
  family=$(lab_shell_family)
  arg=0
  if [ "$family" = ksh93 ]; then arg='#0'; fi # ksh93 reads a bare 0 as a date string
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'printf "%(%Y-%m-%d)T\n" "$1"' sh "$arg"
  case $family in
    bash)
      case $(lab_shell_version) in
        2.* | 3.* | 4.0.* | 4.1.*) [ "$status" -ne 0 ] ;;
        *)
          [ "$status" -eq 0 ]
          [ "$output" = 1970-01-01 ]
          ;;
      esac
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = 1970-01-01 ]
      ;;
    dash | zsh | zsh-sh | yash | posh | mksh | busybox)
      [ "$status" -ne 0 ]
      [ "$output" != 1970-01-01 ]
      ;;
    *) false ;;
  esac
}

@test "an epoch with a leading zero is octal to BSD date -r and decimal elsewhere" {
  export TZ=UTC0
  run_lab "$LAB" fmt 010 %s
  [ "$status" -eq 0 ]
  case $IMPL in
    bsd) [ "$output" = 8 ] ;; # -r parses with strtoimax/strtoq base 0
    gnu | busybox) [ "$output" = 10 ] ;;
    *) false ;;
  esac
}
