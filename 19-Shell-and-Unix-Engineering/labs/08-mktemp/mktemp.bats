#!/usr/bin/env bats
# Tests for Lab 08: mktemp templates, modes, directories, and cleanup.
# Utility behavior is asserted per mktemp implementation (mktemp_flavor), never per shell;
# trap and noclobber behavior is asserted per shell family (lab_shell_family).

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/mktemp-probe.sh"
}

# mktemp_flavor CMD...: print gnu, uutils, busybox, bsd, or unknown for a mktemp command.
mktemp_flavor() {
  local out
  out=$("$@" --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"usage: mktemp"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# mktemp_impls: print each mktemp command installed here, one per line.
mktemp_impls() {
  local cmd
  for cmd in mktemp gmktemp; do
    if command -v "$cmd" >/dev/null 2>&1; then
      printf '%s\n' "$cmd"
    fi
  done
  if command -v busybox >/dev/null 2>&1 \
    && busybox mktemp -u -p "$BATS_TEST_TMPDIR" x.XXXXXX >/dev/null 2>&1; then
    printf '%s\n' "busybox mktemp"
  fi
}

# load_impls: fill the array IMPLS, and fail if there is no mktemp at all.
load_impls() {
  local line list
  list=$(mktemp_impls)
  IMPLS=()
  while IFS= read -r line; do
    [ -n "$line" ] && IMPLS+=("$line")
  done <<<"$list"
  [ "${#IMPLS[@]}" -gt 0 ]
}

@test "new files are 0600 and new directories 0700, minus the umask, in every implementation" {
  local impl i=0 words flavor
  load_impls
  for impl in "${IMPLS[@]}"; do
    i=$((i + 1))
    read -r -a words <<<"$impl"
    flavor=$(mktemp_flavor "${words[@]}")
    echo "impl=$impl flavor=$flavor"
    mkdir "$BATS_TEST_TMPDIR/m$i"
    run_lab "$PROBE" modes "$BATS_TEST_TMPDIR/m$i" "${words[@]}"
    echo "$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu | uutils | bsd | busybox)
        [ "$output" = "022 -rw------- drwx------
0277 -r-------- dr-x------" ]
        ;;
      *) false ;;
    esac
  done
}

@test "ten Xs: GNU, uutils, and BSD randomize all ten; BusyBox randomizes only the last six" {
  local impl i=0 words flavor name
  load_impls
  for impl in "${IMPLS[@]}"; do
    i=$((i + 1))
    read -r -a words <<<"$impl"
    flavor=$(mktemp_flavor "${words[@]}")
    mkdir "$BATS_TEST_TMPDIR/x$i"
    run_lab "$PROBE" try "$BATS_TEST_TMPDIR/x$i" aXXXXXXXXXX "${words[@]}"
    echo "impl=$impl flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $output in
      "ok "*) name=${output#ok } ;;
      *) false ;;
    esac
    [ "${#name}" -eq 11 ]
    case $flavor in
      gnu | uutils | bsd)
        # the first four replaced characters stay XXXX with probability 62^-4
        case $name in
          aXXXX*) false ;;
          a*) true ;;
          *) false ;;
        esac
        ;;
      busybox)
        # the C library's mkstemp replaces exactly six trailing Xs
        case $name in
          aXXXX??????) true ;;
          *) false ;;
        esac
        ;;
      *) false ;;
    esac
  done
}

@test "a template with no X: BSD creates that exact name once; GNU, uutils, and BusyBox refuse" {
  local impl i=0 words flavor
  load_impls
  for impl in "${IMPLS[@]}"; do
    i=$((i + 1))
    read -r -a words <<<"$impl"
    flavor=$(mktemp_flavor "${words[@]}")
    mkdir "$BATS_TEST_TMPDIR/p$i"
    run_lab "$PROBE" try "$BATS_TEST_TMPDIR/p$i" plain "${words[@]}"
    echo "impl=$impl flavor=$flavor first=$output"
    case $flavor in
      bsd)
        [ "$output" = "ok plain" ]
        [ -f "$BATS_TEST_TMPDIR/p$i/plain" ]
        run_lab "$PROBE" try "$BATS_TEST_TMPDIR/p$i" plain "${words[@]}"
        [ "$output" = fail ]
        ;;
      gnu | uutils | busybox)
        [ "$output" = fail ]
        [ ! -e "$BATS_TEST_TMPDIR/p$i/plain" ]
        ;;
      *) false ;;
    esac
  done
}

@test "two Xs: BSD accepts them; GNU and uutils need 3, BusyBox needs 6" {
  local impl i=0 words flavor
  load_impls
  for impl in "${IMPLS[@]}"; do
    i=$((i + 1))
    read -r -a words <<<"$impl"
    flavor=$(mktemp_flavor "${words[@]}")
    mkdir "$BATS_TEST_TMPDIR/t$i"
    run_lab "$PROBE" try "$BATS_TEST_TMPDIR/t$i" aXX "${words[@]}"
    echo "impl=$impl flavor=$flavor output=$output"
    case $flavor in
      bsd)
        case $output in
          "ok a"??) true ;;
          *) false ;;
        esac
        ;;
      gnu | uutils | busybox) [ "$output" = fail ] ;;
      *) false ;;
    esac
    run_lab "$PROBE" try "$BATS_TEST_TMPDIR/t$i" bXXX "${words[@]}"
    echo "three Xs: $output"
    case $flavor in
      bsd | gnu | uutils)
        case $output in
          "ok b"???) true ;;
          *) false ;;
        esac
        ;;
      busybox) [ "$output" = fail ] ;;
      *) false ;;
    esac
  done
}

@test "Xs before a suffix: GNU and uutils randomize them, BSD keeps them literal, BusyBox fails" {
  local impl i=0 words flavor
  load_impls
  for impl in "${IMPLS[@]}"; do
    i=$((i + 1))
    read -r -a words <<<"$impl"
    flavor=$(mktemp_flavor "${words[@]}")
    mkdir "$BATS_TEST_TMPDIR/s$i"
    run_lab "$PROBE" try "$BATS_TEST_TMPDIR/s$i" a.XXXXXX.txt "${words[@]}"
    echo "impl=$impl flavor=$flavor output=$output"
    case $flavor in
      gnu | uutils)
        case $output in
          "ok a.XXXXXX.txt") false ;;
          "ok a."??????".txt") true ;;
          *) false ;;
        esac
        ;;
      bsd) [ "$output" = "ok a.XXXXXX.txt" ] ;;
      busybox) [ "$output" = fail ] ;;
      *) false ;;
    esac
  done
}

@test "no template: GNU, uutils, and BSD use tmp. plus 10 characters; BusyBox uses 6" {
  local impl i=0 words flavor
  load_impls
  for impl in "${IMPLS[@]}"; do
    i=$((i + 1))
    read -r -a words <<<"$impl"
    flavor=$(mktemp_flavor "${words[@]}")
    mkdir "$BATS_TEST_TMPDIR/d$i"
    run_lab "$PROBE" default "$BATS_TEST_TMPDIR/d$i" "${words[@]}"
    echo "impl=$impl flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $output in
      tmp.*) true ;;
      *) false ;;
    esac
    [ -f "$BATS_TEST_TMPDIR/d$i/$output" ]
    case $flavor in
      gnu | uutils | bsd) [ "${#output}" -eq 14 ] ;;
      busybox) [ "${#output}" -eq 10 ] ;;
      *) false ;;
    esac
  done
}

@test "an empty -p falls back to TMPDIR, except BusyBox, which puts the name in the root directory" {
  local impl i=0 words flavor
  load_impls
  for impl in "${IMPLS[@]}"; do
    i=$((i + 1))
    read -r -a words <<<"$impl"
    flavor=$(mktemp_flavor "${words[@]}")
    mkdir "$BATS_TEST_TMPDIR/e$i"
    run_lab "$PROBE" emptyp "$BATS_TEST_TMPDIR/e$i" "${words[@]}"
    echo "impl=$impl flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu | uutils | bsd) [ "$output" = in-TMPDIR ] ;;
      busybox) [ "$output" = root ] ;;
      *) false ;;
    esac
  done
}

@test "the portable idiom makes a 0700 directory under TMPDIR with every implementation" {
  local impl i=0 words flavor
  load_impls
  for impl in "${IMPLS[@]}"; do
    i=$((i + 1))
    read -r -a words <<<"$impl"
    flavor=$(mktemp_flavor "${words[@]}")
    mkdir "$BATS_TEST_TMPDIR/k$i"
    run_lab "$PROBE" scratch "$BATS_TEST_TMPDIR/k$i" "${words[@]}"
    echo "impl=$impl flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu | uutils | bsd | busybox) [ "$output" = "under-TMPDIR drwx------" ] ;;
      *) false ;;
    esac
  done
}

@test "set -C makes > an exclusive create in every shell, and >| overrides it" {
  run_lab "$PROBE" noclobber "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox)
      [ "$output" = "created refused first third" ]
      ;;
    *) false ;;
  esac
}

@test "an EXIT trap removes the scratch directory and keeps the exit status" {
  run_lab "$PROBE" cleanup "$BATS_TEST_TMPDIR" mktemp
  echo "status=$status output=$output"
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox)
      [ "$status" -eq 3 ]
      case $output in
        "$BATS_TEST_TMPDIR"/probe.??????) true ;;
        *) false ;;
      esac
      [ ! -e "$output" ]
      ;;
    *) false ;;
  esac
}

@test "an EXIT trap alone: SIGTERM leaks the directory in dash, zsh, yash, posh, and BusyBox ash" {
  run_lab "$PROBE" leak "$BATS_TEST_TMPDIR" mktemp
  echo "status=$status output=$output"
  case $output in
    "$BATS_TEST_TMPDIR"/probe.??????) true ;;
    *) false ;;
  esac
  case $(lab_shell_family) in
    mksh)
      # mksh runs the EXIT trap, then reports the trap action's status (0)
      [ "$status" -eq 0 ]
      [ ! -e "$output" ]
      ;;
    bash | ksh93)
      [ "$status" -eq 143 ]
      [ ! -e "$output" ]
      ;;
    dash | zsh | zsh-sh | yash | posh | busybox)
      [ "$status" -eq 143 ]
      [ -d "$output" ]
      ;;
    *) false ;;
  esac
}

@test "a TERM trap that calls exit runs the EXIT trap in every shell" {
  run_lab "$PROBE" forward "$BATS_TEST_TMPDIR" mktemp
  echo "status=$status output=$output"
  case $output in
    "$BATS_TEST_TMPDIR"/probe.??????) true ;;
    *) false ;;
  esac
  [ ! -e "$output" ]
  case $(lab_shell_family) in
    mksh) [ "$status" -eq 0 ] ;;
    bash | zsh | zsh-sh | dash | ksh93 | yash | posh | busybox) [ "$status" -eq 143 ] ;;
    *) false ;;
  esac
}

@test "an unknown mode fails with status 2" {
  run_lab "$PROBE" bogus "$BATS_TEST_TMPDIR"
  [ "$status" -eq 2 ]
  [ "$output" = "unknown mode: bogus" ]
}
