#!/usr/bin/env bats
# shellcheck disable=SC2016 # single-quoted code runs in the shell under test, which expands $1
# Sparse files and holes: SEEK_DATA/SEEK_HOLE maps, allocation versus size, hole punching,
# what dd, cp, and cat do with holes, and how shells read the zero bytes a hole returns.

setup() {
  load ../lib/lab
  TOOL="$BATS_TEST_DIRNAME/holes.sh"
  SRC="$BATS_TEST_DIRNAME/holes.c"
  PROBE="$BATS_TEST_TMPDIR/holes"
  run_lab "$TOOL" compile "$SRC" "$PROBE"
  [ "$status" -eq 0 ]
  F="$BATS_TEST_TMPDIR/f"
}

# expect NAME OP NUMBER: test the NAME=VALUE field of $output, as in `expect size -eq 10`.
expect() {
  local value=" $output"
  case $value in
    *" $1="*) ;;
    *) return 1 ;;
  esac
  value=${value#* "$1"=}
  value=${value%% *}
  case $2 in
    -eq) [ "$value" -eq "$3" ] ;;
    -le) [ "$value" -le "$3" ] ;;
    -ge) [ "$value" -ge "$3" ] ;;
    -gt) [ "$value" -gt "$3" ] ;;
    *) return 1 ;;
  esac
}

@test "a write 64 MiB past EOF leaves a hole: full size, one block allocated, zeros read back" {
  run "$PROBE" fill "$F" 67108864 1 x
  [ "$status" -eq 0 ]
  run "$PROBE" stat "$F"
  expect size -eq 67108865
  expect alloc -le 65536
  run_lab_c 'od -An -tx1 -N4 "$1" | tr -d " \n"' sh "$F"
  [ "$output" = 00000000 ]
}

@test "a write 1 MiB past EOF: Linux leaves a hole, APFS zero-fills the gap" {
  run "$PROBE" fill "$F" 1048576 1 x
  [ "$status" -eq 0 ]
  run "$PROBE" stat "$F"
  expect size -eq 1048577
  case $(uname -s) in
    Linux) expect alloc -le 65536 ;;
    Darwin) expect alloc -ge 1048576 ;; # measured on APFS: gaps under 16 MiB are allocated
    *) false ;;
  esac
}

@test "SEEK_DATA and SEEK_HOLE map a file as hole, data, hole" {
  run_lab "$TOOL" extend "$F" 67108864
  [ "$status" -eq 0 ]
  run "$PROBE" fill "$F" 33554432 4096 x
  [ "$status" -eq 0 ]
  run "$PROBE" map "$F"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 4 ]
  [ "${lines[0]}" = "hole 0 33554432" ]
  case ${lines[1]} in
    "data 33554432 "*) ;;
    *) false ;;
  esac
  end=${lines[1]##* }
  [ "$end" -ge 33558528 ]
  [ "$end" -lt 67108864 ]
  [ "${lines[2]}" = "hole $end 67108864" ]
  [ "${lines[3]}" = "end 67108864" ]
}

@test "every file has a virtual hole at EOF, and SEEK_HOLE or SEEK_DATA at EOF fails with ENXIO" {
  run_lab_c 'printf 0123456789 >"$1"' sh "$F"
  [ "$status" -eq 0 ]
  run "$PROBE" edges "$F"
  [ "$status" -eq 0 ]
  [ "$output" = "hole_from_0=10 hole_at_eof=ENXIO data_at_eof=ENXIO" ]
}

@test "dd seek= with empty input sets the size by ftruncate and allocates nothing" {
  run_lab "$TOOL" extend "$F" 1048576
  [ "$status" -eq 0 ]
  run "$PROBE" stat "$F"
  [ "$output" = "size=1048576 alloc=0" ]
  run_lab "$TOOL" sizes "$F"
  [ "$output" = "apparent=1048576 du_k=0" ]
}

@test "dd seek= without conv=notrunc cuts a longer file at the seek point" {
  run_lab_c 'printf "%0100d" 0 >"$1"' sh "$F"
  run_lab "$TOOL" extend "$F" 10
  [ "$status" -eq 0 ]
  run "$PROBE" stat "$F"
  expect size -eq 10
  run_lab_c 'printf "%0100d" 0 >"$1"' sh "$F"
  run_lab "$TOOL" extend-notrunc "$F" 200
  [ "$status" -eq 0 ]
  run "$PROBE" stat "$F"
  expect size -eq 100 # notrunc with no input neither cuts nor extends
}

@test "dd conv=sparse seeks over zero blocks in GNU and BSD dd; BusyBox dd rejects it" {
  run_lab "$TOOL" impl dd
  impl=$output
  run_lab "$TOOL" zeros-sparse "$F" 1024
  case $impl in
    gnu | bsd)
      [ "$status" -eq 0 ]
      run "$PROBE" stat "$F"
      [ "$output" = "size=1048576 alloc=0" ]
      ;;
    busybox)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "cp keeps holes (GNU, macOS); BusyBox cp and cat-with-redirection fill them" {
  run_lab "$TOOL" extend "$F" 67108864
  run "$PROBE" fill "$F" 33554432 4096 x
  [ "$status" -eq 0 ]
  run_lab "$TOOL" copy cat "$F" "$BATS_TEST_TMPDIR/bycat"
  [ "$status" -eq 0 ]
  run "$PROBE" stat "$BATS_TEST_TMPDIR/bycat"
  expect size -eq 67108864
  expect alloc -ge 67108864
  run_lab "$TOOL" impl cp
  impl=$output
  run_lab "$TOOL" copy cp "$F" "$BATS_TEST_TMPDIR/bycp"
  [ "$status" -eq 0 ]
  run "$PROBE" stat "$BATS_TEST_TMPDIR/bycp"
  expect size -eq 67108864
  case $impl in
    gnu | bsd) expect alloc -le 1048576 ;;
    busybox) expect alloc -ge 67108864 ;;
    *) false ;;
  esac
}

@test "punching a hole frees whole blocks and keeps the size; the range reads as zeros" {
  run "$PROBE" fill "$F" 0 196608 x
  [ "$status" -eq 0 ]
  run "$PROBE" punch "$F" 65536 65536
  [ "$status" -eq 0 ]
  [ "$output" = "punch=ok" ]
  run "$PROBE" stat "$F"
  expect size -eq 196608
  expect alloc -le 131072
  run "$PROBE" map "$F"
  [ "${lines[0]}" = "data 0 65536" ]
  [ "${lines[1]}" = "hole 65536 131072" ]
  [ "${lines[2]}" = "data 131072 196608" ]
  run_lab_c 'od -An -tx1 -j65536 -N4 "$1" | tr -d " \n"' sh "$F"
  [ "$output" = 00000000 ]
}

@test "an unaligned punch zeroes the bytes on Linux and fails with EINVAL on macOS" {
  run "$PROBE" fill "$F" 0 8192 x
  run "$PROBE" punch "$F" 100 200
  case $(uname -s) in
    Linux)
      [ "$status" -eq 0 ]
      run_lab_c 'od -An -tx1 -j100 -N2 "$1" | tr -d " \n"; od -An -c -j300 -N1 "$1" | tr -d " \n"' sh "$F"
      [ "$output" = 0000x ]
      ;;
    Darwin)
      [ "$status" -eq 1 ]
      [ "$output" = "punch=EINVAL" ]
      ;;
    *) false ;;
  esac
}

@test "_PC_MIN_HOLE_SIZE exists on macOS and is absent from Linux libc headers" {
  run "$PROBE" minhole "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Linux) [ "$output" = "minhole=undefined" ] ;;
    Darwin) expect minhole -gt 0 ;;
    *) false ;;
  esac
}

@test "reading a hole into a shell variable: shells drop, keep, or stop at the NUL bytes" {
  run "$PROBE" fill "$F" 8 2 a
  [ "$status" -eq 0 ]
  run_lab "$TOOL" comsub-len "$F"
  [ "$status" -eq 0 ]
  last=${lines[${#lines[@]} - 1]}
  case $(lab_shell_family) in
    bash)
      [ "$last" = len=2 ]
      case $(lab_shell_version) in
        [123].* | 4.[0-3]*) [ "${#lines[@]}" -eq 1 ] ;;       # silent drop
        *) [[ $output == *"ignored null byte in input"* ]] ;; # 4.4+ warns on stderr
      esac
      ;;
    dash | busybox | mksh | posh) [ "$output" = len=2 ] ;;
    zsh | zsh-sh) [ "$output" = len=10 ] ;;
    ksh93 | yash) [ "$output" = len=0 ] ;; # the value is a C string that ends at the first NUL
    *) false ;;
  esac
}
