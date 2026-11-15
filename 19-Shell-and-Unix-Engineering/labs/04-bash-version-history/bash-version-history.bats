#!/usr/bin/env bats
# Tests for bash version history: each feature appears exactly at the release whose NEWS
# file announces it, compatibility levels (BASH_COMPAT) restore older behavior, and every
# other shell announces itself through its own version variable instead of BASH_VERSION.

setup() {
  load ../lib/lab
  FEATURES="$BATS_TEST_DIRNAME/bash-features.sh"
  COMPAT="$BATS_TEST_DIRNAME/compat-probe.sh"
  VARS="$BATS_TEST_DIRNAME/version-vars.sh"
}

# bash_num: print the release of the bash under test as MAJOR * 100 + MINOR (5.3 -> 503).
bash_num() {
  local v major rest minor
  v=$(lab_shell_version)
  major=${v%%.*}
  rest=${v#*.}
  minor=${rest%%.*}
  printf '%d\n' "$((major * 100 + minor))"
}

# expect_features SINCE NAME...: with $output from bash-features.sh, each NAME must read
# "yes" when the bash under test is release SINCE or newer, and "no" when it is older.
expect_features() {
  local since=$1 num name want got
  shift
  num=$(bash_num)
  for name in "$@"; do
    if [ "$num" -ge "$since" ]; then want=yes; else want=no; fi
    got=$(printf '%s\n' "$output" | awk -v n="$name" '$1 == n { print $2 }')
    if [ "$got" != "$want" ]; then
      echo "feature $name: got '$got', want '$want' (bash $num, introduced in $since)" >&2
      return 1
    fi
  done
}

# feature_test SINCE NAME...: run the probe and check one release's features in every family.
feature_test() {
  run_lab "$FEATURES"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash) expect_features "$@" ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "the feature probe reports all 33 features once each under bash" {
  run_lab "$FEATURES"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "${#lines[@]}" -eq 33 ]
      # Count distinct names in awk itself, so no pipeline stage can mask a failure.
      distinct=$(printf '%s\n' "$output" | awk '!seen[$1]++ { n++ } END { print n + 0 }') || false
      [ "$distinct" -eq 33 ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "bash 3.0 features: pipefail, =~, {x..y}" {
  feature_test 300 pipefail regex-match brace-sequence
}

@test "bash 3.1 features: += and printf -v" {
  feature_test 301 append-assign printf-v
}

@test "bash 4.0 features: associative arrays, case modification, mapfile, coproc, globstar, |&" {
  feature_test 400 assoc-array case-modify mapfile coproc-keyword globstar pipe-stderr
}

@test "bash 4.1 features: {var} descriptor allocation at 10 or above" {
  feature_test 401 fd-variable
}

@test "bash 4.2 features: declare -g, [[ -v ]], negative index reads, lastpipe, printf %(...)T" {
  feature_test 402 declare-g test-v negative-index-read lastpipe printf-time
}

@test "bash 4.3 features: namerefs and negative index assignment" {
  feature_test 403 nameref negative-index-write
}

@test "bash 4.4 features: \${x@Q}, mapfile -d, local -, inherit_errexit" {
  feature_test 404 transform-Q mapfile-d local-dash inherit-errexit
}

@test "bash 5.0 features: EPOCHSECONDS and BASH_ARGV0" {
  feature_test 500 epochseconds bash-argv0
}

@test "bash 5.1 features: SRANDOM and \${x@U}" {
  feature_test 501 srandom transform-U
}

@test "bash 5.2 features: patsub_replacement on by default, \${a[@]@k}, globskipdots on by default" {
  feature_test 502 patsub-replacement transform-k globskipdots
}

@test "bash 5.3 features: \${ cmd; }, \${| cmd; }, array_expand_once" {
  feature_test 503 nofork-comsub valsub-comsub array-expand-once
}

@test "BASH_VERSINFO has six read-only fields that agree with BASH_VERSION" {
  run_lab "$COMPAT" versinfo
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash) [ "$output" = "fields=6 match=yes readonly=yes" ] ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "a quoted =~ operand matches literally from 3.2; BASH_COMPAT=31 restores regex from 4.3" {
  case $(lab_shell_family) in
    bash)
      num=$(bash_num)
      run_lab "$COMPAT" regex
      [ "$status" -eq 0 ]
      if [ "$num" -ge 302 ]; then [ "$output" = literal ]; else [ "$output" = regex ]; fi
      run_lab "$COMPAT" regex 31
      [ "$status" -eq 0 ]
      if [ "$num" -ge 403 ]; then [ "$output" = regex ]; else [ "$output" = literal ]; fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      run_lab "$COMPAT" regex
      [ "$output" = not-bash ]
      ;;
    *) false ;;
  esac
}

@test "BASH_COMPAT in the initial environment takes effect from 4.4" {
  case $(lab_shell_family) in
    bash)
      num=$(bash_num)
      run_lab "$COMPAT" regex-env 31
      [ "$status" -eq 0 ]
      if [ "$num" -ge 404 ]; then [ "$output" = regex ]; else [ "$output" = literal ]; fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      run_lab "$COMPAT" regex-env 31
      [ "$output" = not-bash ]
      ;;
    *) false ;;
  esac
}

@test "here-strings are pipes from 5.1, and BASH_COMPAT=50 turns them back into files" {
  case $(lab_shell_family) in
    bash)
      num=$(bash_num)
      run_lab "$COMPAT" herestring
      [ "$status" -eq 0 ]
      if [ "$num" -ge 501 ]; then [ "$output" = pipe ]; else [ "$output" = file ]; fi
      run_lab "$COMPAT" herestring 50
      [ "$status" -eq 0 ]
      [ "$output" = file ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      run_lab "$COMPAT" herestring
      [ "$output" = not-bash ]
      ;;
    *) false ;;
  esac
}

@test "BASH_COMPAT rejects a level newer than the running release from 4.3" {
  case $(lab_shell_family) in
    bash)
      num=$(bash_num)
      run_lab "$COMPAT" compat-error 99
      [ "$status" -eq 0 ]
      if [ "$num" -ge 403 ]; then [ "$output" = rejected ]; else [ "$output" = accepted ]; fi
      run_lab "$COMPAT" compat-error 42
      [ "$status" -eq 0 ]
      [ "$output" = accepted ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      run_lab "$COMPAT" compat-error 99
      [ "$output" = not-bash ]
      ;;
    *) false ;;
  esac
}

@test "& in a pattern substitution inserts the match from 5.2 unless patsub_replacement is off" {
  case $(lab_shell_family) in
    bash)
      num=$(bash_num)
      run_lab "$COMPAT" patsub
      [ "$status" -eq 0 ]
      if [ "$num" -ge 502 ]; then [ "$output" = 'a[b]c' ]; else [ "$output" = 'a[&]c' ]; fi
      run_lab "$COMPAT" patsub off
      [ "$status" -eq 0 ]
      if [ "$num" -ge 502 ]; then [ "$output" = 'a[&]c' ]; else [ "$output" = no-option ]; fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      run_lab "$COMPAT" patsub
      [ "$output" = not-bash ]
      ;;
    *) false ;;
  esac
}

@test "\${ cmd; } runs in the current shell from 5.3, where \$(cmd) runs in a subshell" {
  run_lab "$COMPAT" nofork
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "${lines[0]}" = "comsub n=0 x=hi" ]
      num=$(bash_num)
      if [ "$num" -ge 503 ]; then
        [ "${lines[1]}" = "nofork n=1 x=hi" ]
      else
        [ "${lines[1]}" = "nofork unsupported" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "$output" = not-bash ] ;;
    *) false ;;
  esac
}

@test "an unknown probe mode fails with status 2 under bash" {
  run_lab "$COMPAT" no-such-mode
  case $(lab_shell_family) in
    bash) [ "$status" -eq 2 ] ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -eq 0 ]
      [ "$output" = not-bash ]
      ;;
    *) false ;;
  esac
}

@test "each shell announces itself through its own version variable" {
  run_lab "$VARS"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "$output" = "$(printf '%s\n' BASH_VERSION=set ZSH_VERSION=unset KSH_VERSION=unset \
        YASH_VERSION=unset POSH_VERSION=unset BB_ASH_VERSION=unset)" ]
      ;;
    dash)
      [ "$output" = "$(printf '%s\n' BASH_VERSION=unset ZSH_VERSION=unset KSH_VERSION=unset \
        YASH_VERSION=unset POSH_VERSION=unset BB_ASH_VERSION=unset)" ]
      ;;
    zsh | zsh-sh)
      [ "${lines[0]}" = BASH_VERSION=unset ]
      [ "${lines[1]}" = ZSH_VERSION=set ]
      ;;
    ksh93 | mksh)
      [ "${lines[0]}" = BASH_VERSION=unset ]
      [ "${lines[2]}" = KSH_VERSION=set ]
      ;;
    yash)
      [ "${lines[0]}" = BASH_VERSION=unset ]
      [ "${lines[3]}" = YASH_VERSION=set ]
      ;;
    posh)
      [ "${lines[0]}" = BASH_VERSION=unset ]
      [ "${lines[4]}" = POSH_VERSION=set ]
      ;;
    busybox)
      [ "${lines[0]}" = BASH_VERSION=unset ]
      [ "${lines[5]}" = BB_ASH_VERSION=set ]
      ;;
    *) false ;;
  esac
}
