#!/usr/bin/env bats
# Tests for pack.sh (a portable long- and short-option parser), catall.sh (option injection by
# filenames), getopt_probe.c (how each C library's getopt scans argv), and getopts with "-:".
# shellcheck disable=SC2016 # single-quoted code and literal $(...) text belong to the shell under test

setup() {
  load ../lib/lab
  PACK="$BATS_TEST_DIRNAME/pack.sh"
  CATALL="$BATS_TEST_DIRNAME/catall.sh"
}

# build_probe: compile getopt_probe.c into the test's scratch directory; a missing compiler fails.
build_probe() {
  PROBE="$BATS_TEST_TMPDIR/getopt_probe"
  "${CC:-cc}" -std=c99 -Wall -Wextra -Werror -o "$PROBE" "$BATS_TEST_DIRNAME/getopt_probe.c"
}

# probe_libc: print the C library the probe was built against (glibc, apple, musl, or other).
probe_libc() {
  local out
  out=$("$PROBE" short x) || return 1
  printf '%s\n' "$out" | sed -n 's/^libc=//p'
}

@test "worked example: clusters, attached arguments, options after operands, and -- parse as documented" {
  run_lab "$PACK" -vvn -j4 -oout.tar a -x '*.o' b --exclude='*.tmp' -- -c -
  [ "$status" -eq 0 ]
  [ "$output" = "verbose=2
dry_run=1
jobs=4
output=out.tar
color=auto
exclude=<*.o>
exclude=<*.tmp>
operands=4
operand=<a>
operand=<b>
operand=<-c>
operand=<->" ]
}

@test "-- before any operand ends option parsing: every later word is an operand, in order" {
  run_lab "$PACK" -v -- -x a --help -
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "verbose=1" ]
  [ "${lines[5]}" = "operands=4" ]
  [ "${lines[6]}" = "operand=<-x>" ]
  [ "${lines[7]}" = "operand=<a>" ]
  [ "${lines[8]}" = "operand=<--help>" ]
  [ "${lines[9]}" = "operand=<->" ]
}

@test "--help writes usage to stdout only and exits 0" {
  run_lab_c '. "$0" 2>/dev/null' "$PACK" --help
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "Usage: pack [OPTION]... [--] [FILE]..." ]
  run_lab_c '. "$0" 2>&1 >/dev/null' "$PACK" --help
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}

@test "--version and -V print one parseable line on stdout and exit 0" {
  run_lab "$PACK" --version
  [ "$status" -eq 0 ]
  [ "$output" = "pack 1.0.0" ]
  run_lab "$PACK" -vV
  [ "$status" -eq 0 ]
  [ "$output" = "pack 1.0.0" ]
}

@test "usage errors go to stderr only, name the program, and exit 2" {
  run_lab_c '. "$0" 2>&1 >/dev/null' "$PACK" a --frobnicate
  [ "$status" -eq 2 ]
  [ "$output" = "pack: unrecognized option '--frobnicate'
Try 'pack --help' for more information." ]
  run_lab_c '. "$0" 2>/dev/null' "$PACK" a --frobnicate
  [ "$status" -eq 2 ]
  [ "$output" = "" ]
}

@test "an unknown letter inside a cluster is reported by letter" {
  run_lab "$PACK" -vzn
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: invalid option -- 'z'" ]
}

@test "long-option abbreviations are refused, so a later option cannot change their meaning" {
  run_lab "$PACK" --verb
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: unrecognized option '--verb'" ]
}

@test "a missing option-argument is a usage error for short and long forms" {
  run_lab "$PACK" a -o
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: option requires an argument -- 'o'" ]
  run_lab "$PACK" a --output
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: option '--output' requires an argument" ]
}

@test "a flag given =VALUE and an empty --output= are usage errors" {
  run_lab "$PACK" --verbose=3
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: option '--verbose' doesn't allow an argument" ]
  run_lab "$PACK" --output=
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: option '--output' requires a non-empty argument" ]
}

@test "an option-argument that starts with - is taken as the argument, not as an option" {
  run_lab "$PACK" -o -v
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "verbose=0" ]
  [ "${lines[3]}" = "output=-v" ]
}

@test "a repeated scalar option keeps the last value; a repeated list option keeps every value in order" {
  run_lab "$PACK" -o first -x one --output second --exclude two
  [ "$status" -eq 0 ]
  [ "${lines[3]}" = "output=second" ]
  [ "${lines[5]}" = "exclude=<one>" ]
  [ "${lines[6]}" = "exclude=<two>" ]
}

@test "-j validates text before arithmetic: 08 is eight, hex and empty are invalid, 65 and 30 digits are out of range" {
  run_lab "$PACK" -j 08
  [ "$status" -eq 0 ]
  [ "${lines[2]}" = "jobs=8" ]
  run_lab "$PACK" -j 0x10
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: invalid number of jobs: '0x10'" ]
  run_lab "$PACK" --jobs=
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: invalid number of jobs: ''" ]
  run_lab "$PACK" -j 65
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: number of jobs out of range (1 to 64): '65'" ]
  run_lab "$PACK" --jobs 123456789012345678901234567890
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: number of jobs out of range (1 to 64): '123456789012345678901234567890'" ]
}

@test "options after operands are parsed by default and become operands under an exported POSIXLY_CORRECT" {
  run_lab "$PACK" a -v
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "verbose=1" ]
  [ "${lines[5]}" = "operands=1" ]
  # An unexported shell variable (what bash in POSIX mode defines by itself) does not count.
  run_lab_c 'POSIXLY_CORRECT=1; . "$0"' "$PACK" a -v
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "verbose=1" ]
  [ "${lines[5]}" = "operands=1" ]
  run_lab_c 'POSIXLY_CORRECT=1; export POSIXLY_CORRECT; . "$0"' "$PACK" a -v
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "verbose=0" ]
  [ "${lines[5]}" = "operands=2" ]
  [ "${lines[6]}" = "operand=<a>" ]
  [ "${lines[7]}" = "operand=<-v>" ]
}

@test "bash in POSIX mode defines POSIXLY_CORRECT=y without exporting it; the other shells define nothing" {
  probe='printf "%s|" "${POSIXLY_CORRECT-unset}"; env | grep -c "^POSIXLY_CORRECT="'
  case $(lab_shell_family) in
    bash)
      run_lab_c "set -o posix; $probe"
      [ "$output" = "y|0" ]
      ;;
    dash | zsh | zsh-sh | ksh93 | mksh | yash | posh | busybox)
      run_lab_c "$probe"
      [ "$output" = "unset|0" ]
      ;;
    *) false ;;
  esac
}

@test "an optional argument binds only with =, so --color never makes never an operand" {
  run_lab "$PACK" --color never
  [ "$status" -eq 0 ]
  [ "${lines[4]}" = "color=always" ]
  [ "${lines[6]}" = "operand=<never>" ]
  run_lab "$PACK" --color=never
  [ "$status" -eq 0 ]
  [ "${lines[4]}" = "color=never" ]
  run_lab "$PACK" --color=sometimes
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "pack: invalid argument 'sometimes' for '--color' (valid: always, never, auto)" ]
}

@test "operands with blanks, globs, an empty word, and a command substitution survive unevaluated" {
  cd "$BATS_TEST_TMPDIR"
  run_lab "$PACK" 'a b' '' '*' '$(touch pwned)'
  [ "$status" -eq 0 ]
  [ "${lines[5]}" = "operands=4" ]
  [ "${lines[6]}" = "operand=<a b>" ]
  [ "${lines[7]}" = "operand=<>" ]
  [ "${lines[8]}" = "operand=<*>" ]
  [ "${lines[9]}" = 'operand=<$(touch pwned)>' ]
  [ ! -e pwned ]
}

@test "hand-rolled loop bug: shift 2 with one word left loops in bash, zsh, yash, busybox; dash and ksh93 exit" {
  # A guard counter turns the infinite loop into "looping" and status 3, so the test cannot hang.
  run_lab_c 'n=0; while [ "$#" -gt 0 ]; do n=$((n + 1)); if [ "$n" -gt 5 ]; then echo looping; exit 3; fi; case $1 in -j) jobs=$2; shift 2 ;; *) shift ;; esac; done; echo "done jobs=$jobs"' sh -j
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox)
      [ "$status" -eq 3 ]
      [ "${lines[${#lines[@]} - 1]}" = looping ]
      ;;
    dash)
      [ "$status" -eq 2 ]
      [ "$output" = "sh: 1: shift: can't shift that many" ]
      ;;
    ksh93 | mksh | posh)
      [ "$status" -ne 0 ]
      [ "$status" -ne 3 ]
      case $output in
        *looping* | *done*) false ;;
        *) ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "option injection: cat * reads a file named -n as an option; cat -- * and cat ./* read it" {
  mkdir "$BATS_TEST_TMPDIR/d"
  printf 'dash-n-file\n' >"$BATS_TEST_TMPDIR/d/-n"
  printf 'x-file\n' >"$BATS_TEST_TMPDIR/d/x"
  run_lab "$CATALL" naive "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  case $output in
    *dash-n-file*) false ;;
    *1*x-file) ;;
    *) false ;;
  esac
  run_lab "$CATALL" dashdash "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "dash-n-file
x-file" ]
  run_lab "$CATALL" dotslash "$BATS_TEST_TMPDIR/d"
  [ "$status" -eq 0 ]
  [ "$output" = "dash-n-file
x-file" ]
}

@test "getopts accepts - as an option letter, so -: carries long options; OPTARG for a flag differs" {
  run_lab_c 'while getopts "v-:" o; do printf "%s=%s;" "$o" "${OPTARG-unset}"; done; printf "OPTIND=%s\n" "$OPTIND"' sh --verbose=3 -v --dry-run
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | ksh93 | mksh | yash | posh) [ "$output" = "-=verbose=3;v=unset;-=dry-run;OPTIND=4" ] ;;
    dash | zsh | zsh-sh | busybox) [ "$output" = "-=verbose=3;v=;-=dry-run;OPTIND=4" ] ;;
    *) false ;;
  esac
}

@test "getopt(3) permutes options ahead of operands only in glibc" {
  build_probe
  libc=$(probe_libc)
  run_lab_c '"$0" short x a -x' "$PROBE"
  [ "$status" -eq 0 ]
  case $libc in
    glibc) [ "$output" = "libc=glibc
opt=x
optind=2
argv=<-x> <a>" ] ;;
    apple | musl) [ "$output" = "libc=$libc
optind=1
argv=<a> <-x>" ] ;;
    *) false ;;
  esac
}

@test "getopt_long(3) permutes in every libc, and a leading + stops at the first operand in every libc" {
  build_probe
  libc=$(probe_libc)
  run_lab_c '"$0" long x a -x' "$PROBE"
  [ "$status" -eq 0 ]
  [ "$output" = "libc=$libc
opt=x
optind=2
argv=<-x> <a>" ]
  run_lab_c '"$0" long +x a -x' "$PROBE"
  [ "$output" = "libc=$libc
optind=1
argv=<a> <-x>" ]
  run_lab_c '"$0" short +x a -x' "$PROBE"
  [ "$output" = "libc=$libc
optind=1
argv=<a> <-x>" ]
}

@test "POSIXLY_CORRECT stops getopt_long(3) at the first operand in glibc and Apple libc, not in musl" {
  build_probe
  libc=$(probe_libc)
  run_lab_c 'POSIXLY_CORRECT=1; export POSIXLY_CORRECT; "$0" long x a -x' "$PROBE"
  [ "$status" -eq 0 ]
  case $libc in
    glibc | apple) [ "$output" = "libc=$libc
optind=1
argv=<a> <-x>" ] ;;
    musl) [ "$output" = "libc=musl
opt=x
optind=2
argv=<-x> <a>" ] ;;
    *) false ;;
  esac
}

@test "getopt_long(3) accepts a unique prefix and rejects an ambiguous one and =VALUE on a flag" {
  build_probe
  libc=$(probe_libc)
  run_lab_c '"$0" long x --verb' "$PROBE"
  [ "${lines[1]}" = "long=verbose" ]
  run_lab_c '"$0" long x --ver' "$PROBE"
  [ "${lines[1]}" = "opt=? optopt=0" ]
  [ "${lines[2]}" = "optind=2" ]
  run_lab_c '"$0" long x --verbose=1' "$PROBE"
  [ "${lines[1]}" = "opt=? optopt=v" ]
  [ "${lines[0]}" = "libc=$libc" ]
}

@test "getopt(3) takes a next word starting with - as the option-argument; -- is consumed, - is an operand that only glibc permutes past" {
  build_probe
  libc=$(probe_libc)
  run_lab_c '"$0" short o: -o -x' "$PROBE"
  [ "${lines[1]}" = "opt=o arg=-x" ]
  [ "${lines[2]}" = "optind=3" ]
  run_lab_c '"$0" short x -- -x' "$PROBE"
  [ "${lines[1]}" = "optind=2" ]
  run_lab_c '"$0" short x - -x' "$PROBE"
  case $libc in
    glibc)
      [ "${lines[1]}" = "opt=x" ]
      [ "${lines[2]}" = "optind=2" ]
      [ "${lines[3]}" = "argv=<-x> <->" ]
      ;;
    apple | musl)
      [ "${lines[1]}" = "optind=1" ]
      [ "${lines[2]}" = "argv=<-> <-x>" ]
      ;;
    *) false ;;
  esac
}

@test "a missing option-argument leaves optind at argc+1 in Apple libc and musl, at argc in glibc" {
  build_probe
  libc=$(probe_libc)
  run_lab_c '"$0" short :o: -o' "$PROBE"
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "opt=: optopt=o" ]
  case $libc in
    apple | musl) [ "${lines[2]}" = "optind=3" ] ;;
    glibc) [ "${lines[2]}" = "optind=2" ] ;;
    *) false ;;
  esac
}
