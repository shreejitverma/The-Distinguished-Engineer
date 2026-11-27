#!/usr/bin/env bats
# Tests for output interleaving and append atomicity: what O_APPEND, a shared open file description,
# separate descriptors, and stdio buffering do to bytes written by concurrent writers.

# shellcheck disable=SC2016 # the expansions in single quotes belong to the shell under test
setup_file() {
  # shellcheck disable=SC2154 # BATS_FILE_TMPDIR is set by bats
  OIPROBE="$BATS_FILE_TMPDIR/oiprobe"
  "${CC:-cc}" -std=c11 -O2 -Wall -Wextra -Werror -o "$OIPROBE" "$BATS_TEST_DIRNAME/oiprobe.c"
  export OIPROBE
}

setup() {
  load ../lib/lab
  IL="$BATS_TEST_DIRNAME/interleave.sh"
  F="$BATS_TEST_TMPDIR/out"
}

# probe ARGS...: run the oiprobe binary under the shell under test.
probe() {
  run_lab_c '"$@"' probe "$OIPROBE" "$@"
}

@test "four writers using >> lose and split nothing: every one of 200 lines arrives intact" {
  run_lab "$IL" append 4 50 "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "bytes=20000 lines=200 intact=200" ]
}

@test "four writers sharing one open file description lose and split nothing without O_APPEND" {
  run_lab "$IL" shared 4 50 "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "bytes=20000 lines=200 intact=200" ]
}

@test "four writers with a private <> descriptor each overwrite one another: 5000 bytes survive, not 20000" {
  run_lab "$IL" separate 4 50 "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "bytes=5000 lines=50 intact=50" ]
}

@test "four writers into one pipe with one short write per line arrive intact" {
  run_lab "$IL" pipe 4 50 "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "bytes=20000 lines=200 intact=200" ]
}

@test "a >> descriptor appends at the new end after the file is truncated; a > descriptor leaves a hole" {
  run_lab "$IL" holes append "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "bytes=2" ]
  run_lab "$IL" holes plain "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "bytes=7" ]
}

@test "stdout and stderr: >F 2>&1 and >>F 2>>F keep both lines, >F 2>F lets stderr overwrite stdout" {
  run_lab "$IL" merge dup "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "bytes=14 content=stdout-line|e|" ]
  run_lab "$IL" merge twoappend "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "bytes=14 content=stdout-line|e|" ]
  run_lab "$IL" merge twoopen "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "bytes=12 content=e|dout-line|" ]
}

@test "four processes appending 16 single 256 KiB writes each leave 64 uniform blocks and no lost bytes" {
  run_lab "$IL" big "$OIPROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "blocks=64 uniform=64 mixed=0 partial=0 A=16 B=16 C=16 D=16" ]
}

@test "O_APPEND picks the end at write time; seek-to-end then write loses a concurrent append" {
  probe race append "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "size=8 content=BBBBAAAA" ]
  probe race plain "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "size=4 content=AAAA" ]
}

@test "pwrite on an O_APPEND descriptor: Linux appends, macOS honors the offset" {
  probe pwrite-append "$F"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Linux) [ "$output" = "content=0123456789XY" ] ;;
    Darwin) [ "$output" = "content=XY23456789" ] ;;
    *) false ;;
  esac
}

@test "stdio: a 100-byte fwrite is one write(2); a 1500-byte fwrite through a 512-byte buffer is one write on glibc and musl, two on macOS" {
  probe stdio-writes 100 1024
  [ "$status" -eq 0 ]
  [ "$output" = "writes=1 bytes=100 status=0 sizes=100" ]
  probe stdio-writes 1500 512
  [ "$status" -eq 0 ]
  case $(uname -s) in
    # glibc and musl write a request larger than the buffer straight through, unsplit.
    Linux) [ "$output" = "writes=1 bytes=1500 status=0 sizes=1500" ] ;;
    # macOS libc fills a 1,024-byte buffer (it did not honor the 512), then writes the rest.
    Darwin) [ "$output" = "writes=2 bytes=1500 status=0 sizes=1024,476" ] ;;
    *) false ;;
  esac
}

@test "a shell echo or printf of a 100-byte line is exactly one write(2), and /bin/echo is one too" {
  lab_shell_words
  line=$(printf '%99s' '' | tr ' ' A)
  run "$OIPROBE" count-writes "${LAB_SHELL_WORDS[@]}" -c 'echo "$1"' x "$line"
  [ "$status" -eq 0 ]
  [ "$output" = "writes=1 bytes=100 status=0 sizes=100" ]
  run "$OIPROBE" count-writes "${LAB_SHELL_WORDS[@]}" -c 'printf "%s\n" "$1"' x "$line"
  [ "$status" -eq 0 ]
  [ "$output" = "writes=1 bytes=100 status=0 sizes=100" ]
  run "$OIPROBE" count-writes /bin/echo hi
  [ "$status" -eq 0 ]
  [ "$output" = "writes=1 bytes=3 status=0 sizes=3" ]
}

@test "three consecutive echo builtins: one write each, except where the shell batches builtin output" {
  lab_shell_words
  run "$OIPROBE" count-writes "${LAB_SHELL_WORDS[@]}" -c 'echo a; echo b; echo c'
  [ "$status" -eq 0 ]
  [[ "$output" =~ ^writes=([0-9]+)\ bytes=6\ status=0 ]]
  writes=${BASH_REMATCH[1]}
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash) [ "$writes" -eq 3 ] ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) [ "$writes" -ge 1 ] && [ "$writes" -le 3 ] ;;
        *) [ "$writes" -eq 1 ] ;; # ksh93u+ 2012 keeps stdout buffered across builtins
      esac
      ;;
    yash | busybox | posh | mksh) [ "$writes" -ge 1 ] && [ "$writes" -le 3 ] ;;
    *) false ;;
  esac
}

# is_musl: true when the C library on this host is musl (Alpine), whose stdout starts line-buffered.
is_musl() {
  local loader
  for loader in /lib/ld-musl-*; do
    [ -e "$loader" ] && return 0
  done
  return 1
}

@test "a program with stdout on a file and stderr on the same file: buffered stdout lands after unbuffered stderr, except the first line on musl" {
  run_lab_c '"$1" order >"$2" 2>&1; cat "$2"' x "$OIPROBE" "$F"
  [ "$status" -eq 0 ]
  if is_musl; then
    # musl flushes "A" at its newline while stdout is still line-buffered; only "C" is held back.
    [ "$output" = "$(printf 'A\nB\nC')" ]
  else
    [ "$output" = "$(printf 'B\nA\nC')" ]
  fi
  run_lab_c '"$1" order unbuf >"$2" 2>&1; cat "$2"' x "$OIPROBE" "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'A\nB\nC')" ]
}

@test "an unknown mode is rejected with status 2 by both the script and the probe" {
  run_lab "$IL" nosuchmode
  [ "$status" -eq 2 ]
  [[ "$output" == *"unknown mode"* ]]
  probe nosuchmode
  [ "$status" -eq 2 ]
  [[ "$output" == *"usage: oiprobe"* ]]
}
