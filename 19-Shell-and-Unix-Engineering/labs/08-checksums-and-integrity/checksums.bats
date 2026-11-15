#!/usr/bin/env bats
# Lab for Checksums and Integrity: the POSIX cksum CRC, forging it, and how sha256sum -c differs
# between GNU coreutils, BusyBox, and macOS, plus a strict portable manifest verifier.
# shellcheck disable=SC2016 # single-quoted code belongs to the shell under test

setup() {
  load ../lib/lab
  VERIFY=$BATS_TEST_DIRNAME/verify-manifest.sh
  cd "$BATS_TEST_TMPDIR" || return 1
}

# sha_flavor: print which implementation `sha256sum` on PATH is: gnu, darwin, busybox, or
# unknown. The shell under test never changes this, so utility tests branch on it, not on
# lab_shell_family.
sha_flavor() {
  local out
  out=$(sha256sum --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *"(Darwin)"*) echo darwin ;;
    *BusyBox*) echo busybox ;;
    *) echo unknown ;;
  esac
}

# build_forge: compile crcforge.c into the test directory; a missing compiler fails the test.
build_forge() {
  "${CC:-cc}" -std=c99 -O2 -Wall -Wextra -Werror -o crcforge "$BATS_TEST_DIRNAME/crcforge.c"
}

HELLO_SHA256=5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03
X_SHA256=2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881

@test "cksum prints the POSIX CRC and the octet count, and omits the name for stdin" {
  run_lab_c 'printf "hello\n" | cksum; printf "" | cksum'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "3015617425 6" ]
  [ "${lines[1]}" = "4294967295 0" ]
}

@test "crcforge -p reproduces the system cksum for several inputs" {
  build_forge
  printf 'hello\n' >a
  printf '' >b
  awk 'BEGIN { s = "q"; while (length(s) < 70000) s = s s; printf "%s", substr(s, 1, 70000) }' >c
  size=$(wc -c <c)
  [ "$size" -eq 70000 ]
  for f in a b c; do
    run_lab_c 'cksum <"$1"' sh "$f"
    [ "$status" -eq 0 ]
    sys=${output%% *}
    run ./crcforge -p "$f"
    [ "$status" -eq 0 ]
    [ "$output" = "$sys" ]
  done
}

@test "without the length octets, one and two zero bytes have the same CRC; cksum differs" {
  build_forge
  printf '\000' >z1
  printf '\000\000' >z2
  run ./crcforge -n z1
  n1=$output
  run ./crcforge -n z2
  [ "$n1" = 4294967295 ]
  [ "$output" = 4294967295 ]
  run_lab_c 'cksum <z1; cksum <z2'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "4215202376 1" ]
  [ "${lines[1]}" = "4135437457 2" ]
}

@test "four rewritten bytes give a different file the same cksum but a different SHA-256" {
  build_forge
  printf 'approve: alice 0100\n' >target
  printf 'approve:eve 9999ZZZZ' >source
  ./crcforge target source >forged
  run cmp -s target forged
  [ "$status" -eq 1 ]
  run_lab_c 'dd if=forged bs=16 count=1 2>/dev/null; echo'
  [ "$output" = "approve:eve 9999" ]
  run_lab_c 'cksum <target; cksum <forged'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "${lines[1]}" ]
  [ "${lines[0]}" = "3194571458 20" ]
  run_lab_c 'sha256sum <target; sha256sum <forged'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" != "${lines[1]}" ]
}

@test "sha256sum reproduces the FIPS 180-4 abc example" {
  run_lab_c 'printf abc | sha256sum'
  [ "$status" -eq 0 ]
  [ "$output" = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  -" ]
}

@test "sum defaults to the BSD rotate-and-add algorithm in 1 KiB blocks everywhere" {
  run_lab_c 'printf "hello\n" | sum | awk "{ print \$1 + 0, \$2 + 0 }"'
  [ "$status" -eq 0 ]
  [ "$output" = "36979 1" ]
}

@test "sha256sum -c passes a malformed line next to a good one unless --strict" {
  printf 'hello\n' >hello
  printf '%s  hello\nthis line is not a checksum\n' "$HELLO_SHA256" >mixed.sum
  run_lab_c 'sha256sum -c mixed.sum >/dev/null 2>&1; echo "$?"; sha256sum -c --strict mixed.sum >/dev/null 2>&1; echo "$?"'
  [ "$status" -eq 0 ]
  case $(sha_flavor) in
    gnu | darwin) [ "$output" = "0
1" ] ;;
    busybox) [ "$output" = "1
1" ] ;; # the text after the first space is taken as a name whose open fails; --strict is an unknown option
    *) false ;;
  esac
}

@test "sha256sum -c on a file with no checksum lines (an HTML error page)" {
  printf '<!DOCTYPE html>\n<html><body>404 Not Found</body></html>\n' >SHA256SUMS
  run_lab_c 'sha256sum -c SHA256SUMS >/dev/null 2>&1; echo "$?"'
  [ "$status" -eq 0 ]
  case $(sha_flavor) in
    gnu | busybox) [ "$output" = 1 ] ;;
    darwin) [ "$output" = 0 ] ;; # gnu_check() in md5.c never fails for zero parsed lines
    *) false ;;
  esac
}

@test "sha256sum -c on a manifest with CRLF line endings" {
  printf 'hello\n' >hello
  printf '%s  hello\r\n' "$HELLO_SHA256" >crlf.sum
  run_lab_c 'sha256sum -c crlf.sum >/dev/null 2>&1; echo "$?"'
  [ "$status" -eq 0 ]
  case $(sha_flavor) in
    gnu) [ "$output" = 0 ] ;;              # coreutils 9.0+ strips a trailing carriage return
    darwin | busybox) [ "$output" = 1 ] ;; # the name keeps its \r, so the open fails
    *) false ;;
  esac
}

@test "sha256sum escapes a backslash in a file name only in GNU coreutils" {
  printf 'x' >'a\b'
  run_lab_c 'sha256sum "a\\b"'
  [ "$status" -eq 0 ]
  case $(sha_flavor) in
    gnu) [ "$output" = "\\$X_SHA256  a\\\\b" ] ;;
    darwin | busybox) [ "$output" = "$X_SHA256  a\\b" ] ;;
    *) false ;;
  esac
}

@test "verify-manifest accepts a good manifest, including a name with spaces" {
  printf 'hello\n' >hello
  printf 'x' >'my file'
  printf '%s  hello\n%s *my file\n' "$HELLO_SHA256" "$X_SHA256" >m.sum
  run_lab "$VERIFY" m.sum
  [ "$status" -eq 0 ]
  [ "$output" = "hello: OK
my file: OK" ]
}

@test "verify-manifest reads a last line that has no trailing newline" {
  printf 'hello\n' >hello
  printf '%s  hello' "$HELLO_SHA256" >m.sum
  run_lab "$VERIFY" m.sum
  [ "$status" -eq 0 ]
  [ "$output" = "hello: OK" ]
}

@test "verify-manifest reports a tampered file and exits 1" {
  printf 'hellO\n' >hello
  printf '%s  hello\n' "$HELLO_SHA256" >m.sum
  run_lab "$VERIFY" m.sum
  [ "$status" -eq 1 ]
  [ "$output" = "hello: FAILED" ]
}

@test "verify-manifest fails on a malformed line that sha256sum -c would skip" {
  printf 'hello\n' >hello
  printf '%s  hello\nthis line is not a checksum\n' "$HELLO_SHA256" >m.sum
  run_lab "$VERIFY" m.sum
  [ "$status" -eq 1 ]
  [ "$output" = "hello: OK
verify-manifest: m.sum:2: malformed checksum line" ]
}

@test "verify-manifest fails on an empty manifest and on a GNU-escaped line" {
  : >empty.sum
  run_lab "$VERIFY" empty.sum
  [ "$status" -eq 1 ]
  [ "$output" = "verify-manifest: empty.sum: no checksum lines" ]
  printf 'x' >'a\b'
  printf '\\%s  a\\\\b\n' "$X_SHA256" >esc.sum
  run_lab "$VERIFY" esc.sum
  [ "$status" -eq 1 ]
  [ "$output" = "verify-manifest: esc.sum:1: malformed checksum line" ]
}

@test "verify-manifest fails on a listed file that is missing" {
  printf 'hello\n' >hello
  printf '%s  hello\n%s  gone\n' "$HELLO_SHA256" "$X_SHA256" >m.sum
  run_lab "$VERIFY" m.sum
  [ "$status" -eq 1 ]
  [ "$output" = "hello: OK
gone: FAILED open or read" ]
}

@test "verify-manifest refuses absolute and dot-dot names even when the digest matches" {
  mkdir work
  printf 'x' >outside
  printf '%s  ../outside\n%s  %s/outside\n' "$X_SHA256" "$X_SHA256" "$BATS_TEST_TMPDIR" >work/m.sum
  cd work || return 1
  run_lab "$VERIFY" m.sum
  [ "$status" -eq 1 ]
  [ "$output" = "verify-manifest: m.sum:1: unsafe file name
verify-manifest: m.sum:2: unsafe file name" ]
}

@test "verify-manifest exits 2 when the hashing tool is missing" {
  printf 'hello\n' >hello
  printf '%s  hello\n' "$HELLO_SHA256" >m.sum
  export SHA256SUM=no-such-sha256-tool
  run_lab "$VERIFY" m.sum
  [ "$status" -eq 2 ]
  [ "$output" = "verify-manifest: no-such-sha256-tool: not found (set SHA256SUM)" ]
}
