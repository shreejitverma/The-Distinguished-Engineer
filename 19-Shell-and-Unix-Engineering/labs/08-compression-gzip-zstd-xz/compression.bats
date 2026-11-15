#!/usr/bin/env bats
# Compression formats and tools: gzip (RFC 1952), zstd (RFC 8878), and xz (.xz format 1.2.1).
# The probe runs under the shell under test; the compressors are whatever this host provides,
# so gzip behavior is asserted per implementation (compress_probe.sh flavor), not per shell.
# Requires gzip, zstd, and xz (the real XZ Utils, not BusyBox's decompress-only xz applet).

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/compress_probe.sh"
  cd "$BATS_TEST_TMPDIR" || return 1
  printf hello >hello
  FLAVOR=$(sh "$PROBE" flavor)
}

# size FILE: byte count without the padding some wc implementations print.
size() {
  bytes=$(wc -c <"$1")
  echo "$((bytes))"
}

# differ A B: succeed when the two files differ (a bare `! cmd` would not fail a bats test).
differ() {
  if cmp -s "$1" "$2"; then return 1; fi
}

# present NAME...: print each NAME that exists, followed by a space.
present() {
  for name in "$@"; do
    if [ -e "$name" ]; then printf "%s " "$name"; fi
  done
}

@test "the format is identified by its magic bytes, not by the file name" {
  gzip -c hello >a.bin
  zstd -q -c hello >b.bin
  xz -c hello >c.bin
  sh "$PROBE" skippable meta >d.bin
  run_lab "$PROBE" magic a.bin
  [ "$output" = gzip ]
  run_lab "$PROBE" magic b.bin
  [ "$output" = zstd ]
  run_lab "$PROBE" magic c.bin
  [ "$output" = xz ]
  run_lab "$PROBE" magic d.bin
  [ "$output" = zstd-skippable ]
  run_lab "$PROBE" magic hello
  [ "$output" = unknown ]
}

@test "gzip reading a pipe stores no name; only Apple and BSD gzip stamp the current time" {
  printf hello | gzip >pipe.gz
  run_lab "$PROBE" gzip-header pipe.gz
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu | busybox) [ "$output" = "id=1f8b cm=8 flg=00 mtime=0 xfl=0 os=3" ] ;;
    apple | bsd)
      case $output in
        "id=1f8b cm=8 flg=00 mtime="*" xfl=0 os=3") ;;
        *) false ;;
      esac
      mtime=${output#*mtime=}
      mtime=${mtime%% *}
      [ "$mtime" -gt 1700000000 ]
      ;;
    *) false ;;
  esac
}

@test "gzip FILE stores FNAME and MTIME unless -n; BusyBox gzip stores neither" {
  TZ=UTC0 touch -t 202001010000.00 hello
  gzip -c hello >named.gz
  gzip -n -c hello >anon.gz
  run_lab "$PROBE" gzip-header named.gz
  case $FLAVOR in
    gnu | apple | bsd) [ "$output" = "id=1f8b cm=8 flg=08 mtime=1577836800 xfl=0 os=3 name=hello" ] ;;
    busybox) [ "$output" = "id=1f8b cm=8 flg=00 mtime=0 xfl=0 os=3" ] ;;
    *) false ;;
  esac
  run_lab "$PROBE" gzip-header anon.gz
  [ "$output" = "id=1f8b cm=8 flg=00 mtime=0 xfl=0 os=3" ]
}

@test "gzip -n output is byte-identical across mtimes; plain gzip FILE output is not, except BusyBox" {
  TZ=UTC0 touch -t 202001010000.00 hello
  gzip -c hello >t1.gz
  gzip -n -c hello >n1.gz
  TZ=UTC0 touch -t 202101010000.00 hello
  gzip -c hello >t2.gz
  gzip -n -c hello >n2.gz
  cmp -s n1.gz n2.gz
  case $FLAVOR in
    gnu | apple | bsd) differ t1.gz t2.gz ;;
    busybox) cmp -s t1.gz t2.gz ;;
    *) false ;;
  esac
}

@test "the gzip trailer holds CRC-32 of the data and ISIZE, the length modulo 2^32" {
  gzip -n -c hello >hello.gz
  run_lab "$PROBE" gzip-trailer hello.gz
  [ "$output" = "crc32=3610a686 isize=5" ]
  head -c 100000 /dev/zero >zeros
  gzip -n -c zeros >zeros.gz
  run_lab "$PROBE" gzip-trailer zeros.gz
  [ "$output" = "crc32=d411957d isize=100000" ]
}

@test "concatenated gzip members decompress to the concatenation of their data" {
  {
    printf x | gzip
    printf y | gzip
  } >xy.gz
  run gzip -dc xy.gz
  [ "$status" -eq 0 ]
  [ "$output" = xy ]
}

@test "a corrupted CRC fails gzip -t and gzip -dc, but -dc has already written the data" {
  gzip -n -c hello >good.gz
  n=$(size good.gz)
  head -c $((n - 8)) good.gz >bad.gz
  printf '\000\000\000\000\005\000\000\000' >>bad.gz
  run gzip -t bad.gz
  [ "$status" -eq 1 ]
  gzip -dc bad.gz >out 2>/dev/null && false
  out=$(cat out)
  [ "$out" = hello ]
}

@test "a truncated stream fails the decompressor, but the pipeline hides it unless pipefail is set" {
  gzip -n -c hello >good.gz
  n=$(size good.gz)
  head -c $((n - 3)) good.gz >short.gz
  run gzip -dc short.gz
  [ "$status" -eq 1 ]
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'gzip -dc "$1" 2>/dev/null | wc -c >/dev/null; echo "$?"' sh short.gz
  [ "$output" = 0 ]
  run_lab_c 'set -o pipefail'
  if [ "$status" -eq 0 ]; then
    # shellcheck disable=SC2016 # the expansion belongs to the shell under test
    run_lab_c 'set -o pipefail; gzip -dc "$1" 2>/dev/null | wc -c >/dev/null; echo "$?"' sh short.gz
    [ "$output" = 1 ]
  fi
}

@test "trailing bytes after the last member: GNU and BSD warn with status 2, BusyBox ignores them" {
  {
    printf x | gzip
    printf junk
  } >junk.gz
  {
    printf x | gzip
    printf '\000\000\000\000'
  } >nul.gz
  gzip -dc junk.gz >out1 2>/dev/null && s1=0 || s1=$?
  gzip -dc nul.gz >out2 2>/dev/null && s2=0 || s2=$?
  out1=$(cat out1)
  out2=$(cat out2)
  [ "$out1" = x ]
  [ "$out2" = x ]
  case $FLAVOR in
    gnu) [ "$s1:$s2" = 2:0 ] ;;
    apple | bsd) [ "$s1:$s2" = 2:2 ] ;;
    busybox) [ "$s1:$s2" = 0:0 ] ;;
    *) false ;;
  esac
}

@test "DEFLATE's 32 KiB window misses a repeat 40000 bytes back; zstd and xz find it" {
  head -c 30000 /dev/urandom >b30
  cat b30 b30 >r30
  head -c 40000 /dev/urandom >b40
  cat b40 b40 >r40
  gzip -n -c r30 >r30.gz
  gzip -n -c r40 >r40.gz
  zstd -q -c r40 >r40.zst
  xz -T1 -c r40 >r40.xz
  g30=$(size r30.gz)
  g40=$(size r40.gz)
  z40=$(size r40.zst)
  x40=$(size r40.xz)
  [ "$g30" -lt 36000 ]
  [ "$g40" -gt 76000 ]
  [ "$z40" -lt 46000 ]
  [ "$x40" -lt 46000 ]
}

@test "random input grows under every compressor" {
  head -c 65536 /dev/urandom >rand
  gzip -n -c rand >rand.gz
  zstd -q -c rand >rand.zst
  xz -c rand >rand.xz
  g=$(size rand.gz)
  z=$(size rand.zst)
  x=$(size rand.xz)
  [ "$g" -gt 65536 ]
  [ "$z" -gt 65536 ]
  [ "$x" -gt 65536 ]
}

@test "zstd records the content size for a file but not for a pipe, and declares a 2 MiB window" {
  zstd -q -c hello >file.zst
  printf hello | zstd -q -c >pipe.zst
  run_lab "$PROBE" zstd-header file.zst
  [ "$output" = "single_segment=1 checksum=1 content_size=present" ]
  run_lab "$PROBE" zstd-header pipe.zst
  [ "$output" = "single_segment=0 checksum=1 content_size=absent window=2097152" ]
}

@test "the zstd decoder refuses a frame whose window exceeds its memory limit" {
  printf hello | zstd -q -c >pipe.zst
  run zstd -q -dc -M1MB pipe.zst
  [ "$status" -eq 1 ]
  [[ $output == *"too much memory"* ]]
  run zstd -q -dc -M2MB pipe.zst
  [ "$status" -eq 0 ]
  [ "$output" = hello ]
}

@test "zstd frames concatenate, and a skippable frame between them is ignored" {
  {
    zstd -q -c hello
    sh "$PROBE" skippable 'build-id=42'
    printf world | zstd -q -c
  } >cat.zst
  run zstd -q -dc cat.zst
  [ "$status" -eq 0 ]
  [ "$output" = helloworld ]
}

@test "zstd and xz reject trailing junk; xz --single-stream ignores it" {
  {
    zstd -q -c hello
    printf junk
  } >junk.zst
  {
    xz -c hello
    printf junk
  } >junk.xz
  zstd -q -dc junk.zst >out1 2>/dev/null && false
  out1=$(cat out1)
  [ "$out1" = hello ]
  xz -dc junk.xz >out2 2>/dev/null && false
  out2=$(cat out2)
  [ "$out2" = hello ]
  run xz -dc --single-stream junk.xz
  [ "$status" -eq 0 ]
  [ "$output" = hello ]
}

@test "xz Stream Padding must be NUL bytes in a multiple of four" {
  xz -c hello >h.xz
  cat h.xz >p4.xz
  printf '\000\000\000\000' >>p4.xz
  cat h.xz >p3.xz
  printf '\000\000\000' >>p3.xz
  run xz -dc p4.xz
  [ "$status" -eq 0 ]
  [ "$output" = hello ]
  run xz -dc p3.xz
  [ "$status" -eq 1 ]
}

@test "xz defaults to a CRC64 check, ends in YZ, and is a multiple of four bytes long" {
  xz -c hello >h.xz
  xz -C crc32 -c hello >c.xz
  run_lab "$PROBE" xz-flags h.xz
  [ "$output" = "check=crc64 footer=YZ size_mod4=0" ]
  run_lab "$PROBE" xz-flags c.xz
  [ "$output" = "check=crc32 footer=YZ size_mod4=0" ]
}

@test "gzip and xz replace the input file; zstd keeps it unless --rm" {
  printf data >g
  printf data >z
  printf data >x
  gzip g
  zstd -q z
  xz x
  left=$(present g g.gz z z.zst x x.xz)
  [ "$left" = "g.gz z z.zst x.xz " ]
}

@test "a second hard link stops gzip and xz from replacing the file, but not BusyBox gzip or zstd --rm" {
  printf data >g
  ln g g2
  printf data >x
  ln x x2
  printf data >z
  ln z z2
  gzip g 2>/dev/null && gs=0 || gs=$?
  xz x 2>/dev/null && xs=0 || xs=$?
  zstd -q --rm z
  [ "$xs" = 2 ]
  [ -e x ]
  [ ! -e x.xz ]
  [ ! -e z ]
  [ -e z.zst ]
  [ -e z2 ]
  left=$(present g g.gz)
  case $FLAVOR in
    gnu) [ "$gs:$left" = "2:g " ] ;;
    apple | bsd) [ "$gs:$left" = "1:g " ] ;;
    busybox) [ "$gs:$left" = "0:g.gz " ] ;;
    *) false ;;
  esac
}

@test "xz multithreaded output differs from -T1 and does not depend on the thread count" {
  xz -T1 -c hello >t1.xz
  xz -T2 -c hello >t2.xz
  xz -T4 -c hello >t4.xz
  xz -c hello >td.xz
  differ t1.xz t2.xz
  cmp -s t2.xz t4.xz
  xz --version >ver
  version=$(sed -n '1s/.* \([0-9][0-9]*\)\.\([0-9][0-9]*\)\..*/\1 \2/p' ver)
  # shellcheck disable=SC2086,SC2248 # split "MAJOR MINOR" into two positional parameters
  set -- $version
  # xz 5.6.0 made multithreaded mode the default; 5.4.x and older default to -T1.
  if [ "$1" -gt 5 ] || { [ "$1" -eq 5 ] && [ "$2" -ge 6 ]; }; then
    cmp -s td.xz t2.xz
  else
    cmp -s td.xz t1.xz
  fi
}

@test "zstd output is the same for -T1 and -T4, but --single-thread differs" {
  awk 'BEGIN { s = 1; for (i = 0; i < 400000; i++) { s = (s * 69069 + 1) % 4294967296; printf "%08d host=%d user=%d op=%s\n", i, s % 97, s % 1009, (s % 3 ? "read" : "write") } }' >logs
  n=$(size logs)
  [ "$n" -gt 12000000 ]
  zstd -q -c -T1 logs >t1.zst
  zstd -q -c -T4 logs >t4.zst
  zstd -q -c --single-thread logs >st.zst
  cmp -s t1.zst t4.zst
  differ t1.zst st.zst
}
