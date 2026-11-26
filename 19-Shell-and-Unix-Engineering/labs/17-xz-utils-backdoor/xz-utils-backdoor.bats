#!/usr/bin/env bats
# Tests for the defanged model of the xz-utils 5.6.x build hook: what the git tree holds, what only the
# tarball adds, how the hook finds, decodes and runs its data, and how to detect that shape.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  FX="$BATS_TEST_TMPDIR/fx"
  export RPM_ARCH=none
}

# fixture: build DIR/git, DIR/tarball and DIR/ref under the test's temp directory.
fixture() {
  run_lab "$LAB/mkfixture.sh" "$FX"
  [ "$status" -eq 0 ]
}

MAP_560='\5-\51\204-\377\52-\115\132-\203\0-\4\116-\131'
MAP_561='\114-\321\322-\377\35-\47\14-\34\0-\13\50-\113'

@test "the tr swap of tab/space and dash/underscore is an involution" {
  run_lab_c 'printf "a b\tc-d_e\n" | LC_ALL=C tr "\t \-_" " \t_\-"'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'a\tb c_d-e')" ]
  run_lab_c 'printf "a b\tc-d_e\n" | LC_ALL=C tr "\t \-_" " \t_\-" | LC_ALL=C tr "\t \-_" " \t_\-"'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'a b\tc-d_e')" ]
}

@test "sed r\\n copies its input unchanged and creates no file" {
  mkdir -p "$BATS_TEST_TMPDIR/sedr"
  cd "$BATS_TEST_TMPDIR/sedr"
  printf 'one\ntwo\n' >in
  run_lab_c 'sed "r\n" in >out; cmp in out; ls'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'in\nout')" ]
}

@test "tail -c +N starts at byte N, counting from 1" {
  run_lab_c 'printf 0123456789 | tail -c +4'
  [ "$status" -eq 0 ]
  [ "$output" = "3456789" ]
}

@test "the 5.6.0 and 5.6.1 tr maps are permutations of all 256 byte values" {
  run_lab "$LAB/tr-map-probe.sh" "$MAP_560"
  [ "$status" -eq 0 ]
  [ "$output" = "distinct=256 first=239 240 241 242 243 0" ]
  run_lab "$LAB/tr-map-probe.sh" "$MAP_561"
  [ "$status" -eq 0 ]
  [ "$output" = "distinct=256 first=208 209 210 211 212 213" ]
}

@test "a head -c skip-and-keep chain works on seekable input in every head" {
  printf '0123456789ABCDEFGHIJKLMNOPQRSTUV' >"$BATS_TEST_TMPDIR/in.txt"
  run_lab "$LAB/head-chain-probe.sh" file "$BATS_TEST_TMPDIR/in.txt"
  [ "$status" -eq 0 ]
  [ "$output" = "456789ABGHIJKLMN" ]
}

@test "the same chain on a pipe needs a head that reads no more than it needs" {
  local head_version system
  printf '0123456789ABCDEFGHIJKLMNOPQRSTUV' >"$BATS_TEST_TMPDIR/in.txt"
  run_lab "$LAB/head-chain-probe.sh" pipe "$BATS_TEST_TMPDIR/in.txt"
  [ "$status" -eq 0 ]
  system=$(uname)
  if head_version=$(head --version 2>&1) && [[ $head_version == *'GNU coreutils'* ]]; then
    [ "$output" = "456789ABGHIJKLMN" ]
  elif [ "$system" = Darwin ]; then
    [ -z "$output" ]
  else
    skip "pipe behaviour of this head is not measured; only GNU coreutils and macOS are asserted"
  fi
}

@test "the architecture gate as written exits only for non-x86_64 linux-gnu builds" {
  run_lab "$LAB/gate.sh" x86_64-pc-linux-gnu
  [ "$output" = "as-written=continue intended=continue" ]
  run_lab "$LAB/gate.sh" aarch64-unknown-linux-gnu
  [ "$output" = "as-written=exit intended=exit" ]
  run_lab "$LAB/gate.sh" x86_64-apple-darwin23
  [ "$output" = "as-written=continue intended=exit" ]
  run_lab "$LAB/gate.sh" x86_64-pc-linux-musl
  [ "$output" = "as-written=continue intended=exit" ]
}

@test "the git tree and the tarball differ by generated files and one macro" {
  fixture
  run_lab "$LAB/distdiff.sh" "$FX/git" "$FX/tarball" "$FX/ref"
  [ "$status" -eq 1 ]
  [ "$output" = "$(printf 'EXTRA aclocal.m4 no-reference\nEXTRA configure no-reference\nEXTRA m4/build-to-host.m4 DIFFERS')" ]
  run_lab "$LAB/distdiff.sh" "$FX/git" "$FX/git"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "the data files are inert in git: no macro, no hook, no flag" {
  fixture
  mkdir -p "$FX/git/debian" "$FX/build"
  : >"$FX/git/debian/rules"
  [ -f "$FX/git/tests/files/bad-3-corrupt_model.gzip" ]
  [ -f "$FX/git/tests/files/good-large_compressed.gz" ]
  run_lab "$LAB/build-to-host-analog.sh" "$FX/git" "$FX/build"
  [ "$status" -eq 0 ]
  [ "$output" = "no build-to-host.m4: hook absent" ]
  [ ! -e "$FX/build/injected.flag" ]
}

@test "the committed files alone decode to the stage-1 script the tarball hook runs" {
  fixture
  mkdir -p "$FX/build"
  run_lab "$LAB/build-to-host-analog.sh" --dump-stage1 "$FX/tarball" "$FX/build"
  [ "$status" -eq 0 ]
  via_hook=$output
  # shellcheck disable=SC2016 # the probe shell expands its own positional parameter
  run_lab_c 'LC_ALL=C; export LC_ALL; sed "r\n" "$1" | tr "\t \-_" " \t_\-" | tail -c +15 | gzip -d' sh "$FX/git/tests/files/bad-3-corrupt_model.gzip"
  [ "$status" -eq 0 ]
  [ "$output" = "$via_hook" ]
  case $output in
    *'| /bin/sh'*) ;;
    *) false ;;
  esac
}

@test "the marker line is plain text in the raw file while the script is not" {
  fixture
  raw="$FX/git/tests/files/bad-3-corrupt_model.gzip"
  # shellcheck disable=SC2016 # the probe shell expands its own positional parameter
  run_lab_c 'LC_ALL=C grep -c "^####Hello####\$" "$1"' sh "$raw"
  [ "$output" = 1 ]
  # shellcheck disable=SC2016 # the probe shell expands its own positional parameter
  run_lab_c 'LC_ALL=C grep -c "gzip -dc" "$1"' sh "$raw"
  [ "$output" = 0 ]
  # shellcheck disable=SC2016 # the probe shell expands its own positional parameter
  run_lab_c 'LC_ALL=C grep -c "bad-3" "$1"' sh "$FX/tarball/m4/build-to-host.m4"
  [ "$output" = 0 ]
}

@test "the hook injects on a Debian-style build" {
  local flag
  fixture
  mkdir -p "$FX/tarball/debian" "$FX/build"
  : >"$FX/tarball/debian/rules"
  run_lab "$LAB/build-to-host-analog.sh" "$FX/tarball" "$FX/build"
  [ "$status" -eq 0 ]
  [ "$output" = "hook ran" ]
  flag=$(cat "$FX/build/injected.flag")
  [ "$flag" = injected ]
  [ ! -e "$FX/build/carrier.tmp" ]
}

@test "the hook injects when RPM_ARCH is x86_64 and stays quiet otherwise" {
  local flag
  fixture
  mkdir -p "$FX/build-rpm" "$FX/build-plain"
  export RPM_ARCH=x86_64
  run_lab "$LAB/build-to-host-analog.sh" "$FX/tarball" "$FX/build-rpm"
  [ "$status" -eq 0 ]
  flag=$(cat "$FX/build-rpm/injected.flag")
  [ "$flag" = injected ]
  export RPM_ARCH=none
  run_lab "$LAB/build-to-host-analog.sh" "$FX/tarball" "$FX/build-plain"
  [ "$status" -eq 0 ]
  [ "$output" = "hook ran" ]
  [ ! -e "$FX/build-plain/injected.flag" ]
}

@test "the scanner flags the injected macro and passes the clean one" {
  fixture
  run_lab "$LAB/scan.sh" "$FX/tarball"
  [ "$status" -eq 1 ]
  case $output in
    *"m4/build-to-host.m4:14:EVAL-TO-SHELL"*) ;;
    *) false ;;
  esac
  case $output in
    *"m4/build-to-host.m4:10:MARKER-DISCOVERY"*) ;;
    *) false ;;
  esac
  run_lab "$LAB/scan.sh" "$FX/ref"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  run_lab "$LAB/scan.sh" "$FX/git"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "the scanner flags a decoded stage-1 script and rejects bad usage" {
  fixture
  mkdir -p "$FX/build" "$FX/dumped"
  run_lab "$LAB/build-to-host-analog.sh" --dump-stage1 "$FX/tarball" "$FX/build"
  [ "$status" -eq 0 ]
  printf '%s\n' "$output" >"$FX/dumped/stage1.sh"
  run_lab "$LAB/scan.sh" "$FX/dumped"
  [ "$status" -eq 1 ]
  case $output in
    *"stage1.sh:1:SKIP-CHAIN"*) ;;
    *) false ;;
  esac
  case $output in
    *"stage1.sh:3:DECOMPRESS-TO-SHELL"*) ;;
    *) false ;;
  esac
  run_lab "$LAB/scan.sh"
  [ "$status" -eq 2 ]
  run_lab "$LAB/scan.sh" "$FX/no-such-dir"
  [ "$status" -eq 2 ]
}

@test "the other scripts reject bad usage with status 2" {
  run_lab "$LAB/mkfixture.sh"
  [ "$status" -eq 2 ]
  run_lab "$LAB/build-to-host-analog.sh" onlyone
  [ "$status" -eq 2 ]
  run_lab "$LAB/gate.sh"
  [ "$status" -eq 2 ]
  run_lab "$LAB/tr-map-probe.sh"
  [ "$status" -eq 2 ]
  run_lab "$LAB/head-chain-probe.sh" bogus "$BATS_TEST_TMPDIR/none"
  [ "$status" -eq 2 ]
  run_lab "$LAB/distdiff.sh" onlyone
  [ "$status" -eq 2 ]
}

@test "a test with no spaces around = is a one-word test, so the 5.6.1 Linux check never fires" {
  # The injected 5.6.1 line was: [ ! $(uname)="Linux" ] && exit 0, which expands to one non-empty word after !.
  run_lab_c '[ ! x=y ] && exit 7; echo continued'
  [ "$status" -eq 0 ]
  [ "$output" = continued ]
  run_lab_c '[ ! x = y ] && exit 7; echo continued'
  [ "$status" -eq 7 ]
  [ -z "$output" ]
}
