#!/usr/bin/env bats
# tar and archives: the ustar header on disk, how each implementation stores names that do
# not fit it, and what GNU tar, bsdtar (libarchive), and BusyBox tar do with hostile archives.
# The shell under test runs the probe; the tar on PATH decides the behavior, so the tests
# branch on the tar implementation, not on the shell family.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/tarlab.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
  "${CC:-cc}" -Wall -Wextra -Werror -o "$BATS_TEST_TMPDIR/mktar" "$BATS_TEST_DIRNAME/mktar.c"
  export MKTAR="$BATS_TEST_TMPDIR/mktar"
}

# flavor: print gnu, bsd, or busybox for the tar on PATH, as the probe itself detects it.
flavor() {
  run_lab "$PROBE" flavor
  [ "$status" -eq 0 ]
  printf '%s\n' "$output"
}

@test "the probe recognizes the tar on PATH" {
  run_lab "$PROBE" flavor
  [ "$status" -eq 0 ]
  case $output in
    gnu | bsd | busybox) true ;;
    *) false ;;
  esac
}

@test "a one-file archive: header fields, a POSIX checksum that verifies, and the total size" {
  impl=$(flavor)
  run_lab "$PROBE" header
  [ "$status" -eq 0 ]
  case $impl in
    bsd) [ "$output" = 'type=0 name=hello.txt prefix_len=0 magic=ustar\00000 size=3 chksum=ok bytes=2048' ] ;;
    gnu) [ "$output" = 'type=0 name=hello.txt prefix_len=0 magic=ustar\040\040\000 size=3 chksum=ok bytes=10240' ] ;;
    busybox) [ "$output" = 'type=0 name=hello.txt prefix_len=0 magic=ustar\040\040\000 size=3 chksum=ok bytes=2048' ] ;;
    *) false ;;
  esac
}

@test "a 120-byte name: pax extended header (bsdtar) or GNU LongLink (GNU, BusyBox)" {
  impl=$(flavor)
  run_lab "$PROBE" longname
  [ "$status" -eq 0 ]
  case $impl in
    bsd) [ "$output" = 'type=x name=PaxHeader/aaa prefix_len=0 magic=ustar\00000 size=160 chksum=ok roundtrip=yes' ] ;;
    gnu | busybox) [ "$output" = 'type=L name=././@LongLink prefix_len=0 magic=ustar\040\040\000 size=121 chksum=ok roundtrip=yes' ] ;;
    *) false ;;
  esac
}

@test "a 121-byte path that splits at a slash: ustar prefix (bsdtar) or LongLink (GNU, BusyBox)" {
  impl=$(flavor)
  run_lab "$PROBE" prefix
  [ "$status" -eq 0 ]
  case $impl in
    bsd) [ "$output" = 'type=0 name=fffffffffffff prefix_len=60 magic=ustar\00000 size=0 chksum=ok roundtrip=yes' ] ;;
    gnu | busybox) [ "$output" = 'type=L name=././@LongLink prefix_len=0 magic=ustar\040\040\000 size=122 chksum=ok roundtrip=yes' ] ;;
    *) false ;;
  esac
}

@test "a member named ../escape never escapes: skipped with an error (GNU, bsdtar) or stripped (BusyBox)" {
  impl=$(flavor)
  run_lab "$PROBE" dotdot
  [ "$status" -eq 0 ]
  case $impl in
    gnu) [ "$output" = "rc=2 escaped=no stripped=no ok=yes" ] ;;
    bsd) [ "$output" = "rc=1 escaped=no stripped=no ok=yes" ] ;;
    busybox) [ "$output" = "rc=0 escaped=no stripped=yes ok=yes" ] ;;
    *) false ;;
  esac
}

@test "an absolute member name loses its leading slash and lands under the extraction directory" {
  impl=$(flavor)
  run_lab "$PROBE" absolute
  [ "$status" -eq 0 ]
  case $impl in
    gnu | bsd | busybox) [ "$output" = "rc=0 written=no stripped=yes" ] ;;
    *) false ;;
  esac
}

@test "one archive with a symlink and a file beneath it cannot write outside" {
  impl=$(flavor)
  run_lab "$PROBE" symlink-one
  [ "$status" -eq 0 ]
  case $impl in
    gnu) [ "$output" = "rc=2 escaped=no esc=symlink" ] ;; # placeholder file, symlink made at the end
    bsd) [ "$output" = "rc=1 escaped=no esc=symlink" ] ;; # refuses to extract through a symlink
    busybox) [ "$output" = "rc=1 escaped=no esc=dir" ] ;; # deferred symlink collides with the made directory
    *) false ;;
  esac
}

@test "two archives into one directory: GNU and BusyBox write through the planted symlink, bsdtar refuses" {
  impl=$(flavor)
  run_lab "$PROBE" symlink-two
  [ "$status" -eq 0 ]
  case $impl in
    gnu | busybox) [ "$output" = "rc_a=0 rc_b=0 escaped=yes esc=symlink" ] ;;
    bsd) [ "$output" = "rc_a=0 rc_b=1 escaped=no esc=symlink" ] ;;
    *) false ;;
  esac
}

@test "a file named --exclude=a becomes an option under a bare glob; -- or ./* keeps it a name" {
  impl=$(flavor)
  run_lab "$PROBE" glob
  [ "$status" -eq 0 ]
  case $impl in
    gnu | bsd | busybox) [ "$output" = "star=b dashdash=--exclude=a a b dotslash=./--exclude=a ./a ./b" ] ;;
    *) false ;;
  esac
}

@test "concatenated archives list only the first archive unless zero blocks are ignored" {
  impl=$(flavor)
  run_lab "$PROBE" concat
  [ "$status" -eq 0 ]
  case $impl in
    gnu | bsd) [ "$output" = "plain=one ignore_zeros=one two" ] ;;
    busybox) [ "$output" = "plain=one ignore_zeros=error" ] ;;
    *) false ;;
  esac
}

@test "a non-root extraction applies the umask and drops set-user-ID in all three" {
  impl=$(flavor)
  run_lab "$PROBE" perms
  [ "$status" -eq 0 ]
  case $impl in
    # BusyBox skips its chmod when getuid() != 0 (archival/tar.c, "bug 3844").
    gnu | bsd | busybox) [ "$output" = "rc=0 suid=-rwxr-xr-x open=-rwxr-xr-x" ] ;;
    *) false ;;
  esac
}

@test "pinned mtimes, a sorted list, and no recursion make two archives byte-identical" {
  impl=$(flavor)
  run_lab "$PROBE" repro
  [ "$status" -eq 0 ]
  case $impl in
    gnu | bsd | busybox) [ "$output" = "naive=differ pinned=same members=./a ./d/ ./d/b" ] ;;
    *) false ;;
  esac
}

@test "an unknown mode is a usage error" {
  run_lab "$PROBE" no-such-mode
  [ "$status" -eq 2 ]
  [[ $output == *usage:* ]]
}
