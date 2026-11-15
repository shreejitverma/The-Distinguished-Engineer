#!/usr/bin/env bats
# Tests for ddprobe.sh: what the dd on PATH (or $DD) does with short reads,
# block accounting, skip and seek, number syntax, conversions, and signals.
# The shell under test never changes which dd runs, so expectations branch on
# the dd implementation (gnu, bsd, busybox, uutils), not on the shell family.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/ddprobe.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

# probe_flavor: set FLAVOR to the dd implementation the probe detects.
probe_flavor() {
  run_lab "$PROBE" flavor
  [ "$status" -eq 0 ]
  FLAVOR=$output
}

# has_siginfo: succeed when this platform defines SIGINFO (macOS, the BSDs).
has_siginfo() {
  kill -l INFO >/dev/null 2>&1
}

@test "the probe recognizes the dd implementation" {
  probe_flavor
  case $FLAVOR in
    gnu | bsd | busybox | uutils) true ;;
    *) false ;;
  esac
}

@test "count= counts reads, so a short pipe read copies half; iflag=fullblock fills the block" {
  probe_flavor
  run_lab "$PROBE" shortread
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu | bsd | busybox | uutils) [ "$output" = "plain=4 fullblock=8" ] ;;
    *) false ;;
  esac
}

@test "bs= writes each short read as its own block; ibs= and obs= aggregate (not in BusyBox)" {
  probe_flavor
  run_lab "$PROBE" records
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu | bsd | uutils)
      [ "$output" = "ibs-obs: in=0+2 out=1+0
bs: in=0+2 out=0+2
default: in=0+2 out=0+1" ]
      ;;
    busybox)
      # One buffer whenever ibs equals obs, so short reads are never aggregated.
      [ "$output" = "ibs-obs: in=0+2 out=0+2
bs: in=0+2 out=0+2
default: in=0+2 out=0+2" ]
      ;;
    *) false ;;
  esac
}

@test "skip= on a pipe counts reads in GNU and BusyBox, bytes in BSD and uutils" {
  probe_flavor
  run_lab "$PROBE" skip
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu | busybox) [ "$output" = "plain=cdefgh fullblock=efgh" ] ;;
    bsd | uutils) [ "$output" = "plain=efgh fullblock=efgh" ] ;;
    *) false ;;
  esac
}

@test "skip= past the end of a pipe is fatal only in BSD dd" {
  probe_flavor
  run_lab "$PROBE" skipeof
  [ "$status" -eq 0 ]
  case $FLAVOR in
    bsd) [ "$output" = "status=1" ] ;;
    gnu | busybox | uutils) [ "$output" = "status=0" ] ;;
    *) false ;;
  esac
}

@test "seek= keeps the blocks it skips and truncates the rest unless conv=notrunc" {
  probe_flavor
  run_lab "$PROBE" seek
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu | bsd | busybox | uutils)
      [ "$output" = "trunc=01AB notrunc=01AB456789 grow=8 nuls=4" ]
      ;;
    *) false ;;
  esac
}

@test "number syntax: BSD reads 010 as octal and 0x10 as hex; B and w differ" {
  probe_flavor
  run_lab "$PROBE" numbers
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu) [ "$output" = "octal=10 hex=0 product=8 B=2 w=2 zero=0" ] ;;
    bsd) [ "$output" = "octal=8 hex=16 product=8 B=1024 w=4 zero=0" ] ;;
    busybox) [ "$output" = "octal=10 hex=0 product=0 B=0 w=2 zero=0" ] ;;
    uutils) [ "$output" = "octal=8 hex=0 product=8 B=2 w=2 zero=0" ] ;;
    *) false ;;
  esac
}

@test "conv=swab keeps an odd last byte, conv=sync pads with NULs, ucase is absent in BusyBox" {
  probe_flavor
  run_lab "$PROBE" conv
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu | bsd | uutils) [ "$output" = "swab=bac ucase=ABC sync=8/5" ] ;;
    busybox) [ "$output" = "swab=bac ucase= sync=8/5" ] ;;
    *) false ;;
  esac
}

@test "every dd rejects the iflags= spelling printed in POSIX.1-2024 and accepts iflag=" {
  probe_flavor
  run_lab "$PROBE" spelling
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu | bsd | busybox | uutils) [ "$output" = "iflags=rejected iflag=accepted" ] ;;
    *) false ;;
  esac
}

@test "SIGUSR1 reports statistics only where SIGINFO does not exist; elsewhere it kills dd" {
  probe_flavor
  run_lab "$PROBE" signal USR1
  [ "$status" -eq 0 ]
  if has_siginfo; then
    # GNU dd catches SIGINFO instead; BSD dd never catches USR1.
    case $FLAVOR in
      gnu | bsd | uutils) [ "$output" = "killed reports=0" ] ;;
      *) false ;;
    esac
  else
    case $FLAVOR in
      gnu | busybox) [ "$output" = "survived reports=2" ] ;;
      # uutils installs its handler with SA_RESTART and polls the flag only after
      # a block is copied, so the blocked read never reports before EOF.
      uutils) [ "$output" = "survived reports=1" ] ;;
      *) false ;;
    esac
  fi
}

@test "SIGINFO: GNU reports at once, BSD at the next block; without SIGINFO nothing is sent" {
  probe_flavor
  run_lab "$PROBE" signal INFO
  [ "$status" -eq 0 ]
  if has_siginfo; then
    case $FLAVOR in
      # GNU's handler has no SA_RESTART, so the blocked read returns EINTR and
      # dd prints at once; BSD's signal() restarts the read, so the report
      # waits for a block that never comes and only the final one appears.
      gnu) [ "$output" = "survived reports=2" ] ;;
      bsd) [ "$output" = "survived reports=1" ] ;;
      # uutils catches no SIGINFO; its default action on macOS and the BSDs
      # discards the signal, so dd keeps running and reports once at EOF.
      uutils) [ "$output" = "survived reports=1" ] ;;
      *) false ;;
    esac
  else
    # kill -s INFO fails, so dd only writes its final report at EOF.
    case $FLAVOR in
      gnu | busybox | uutils) [ "$output" = "survived reports=1" ] ;;
      *) false ;;
    esac
  fi
}

@test "an unknown mode is a usage error" {
  run_lab "$PROBE" nosuchmode
  [ "$status" -eq 2 ]
  [[ $output == usage:* ]]
}
