#!/usr/bin/env bats
# Tests for traps.sh: cross-userland divergences, option parsing, and POSIX idioms.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/traps.sh"
}

userland_flavor() {
  local out
  out=$(ls --version 2>&1) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *"uutils"*) echo uutils ;;
    *"BusyBox"*) echo busybox ;;
    *"unrecognized option"*"usage: ls"* | *"illegal option"*"usage: ls"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

@test "userland flavor is detected" {
  flavor=$(userland_flavor)
  case $flavor in
    gnu | bsd | busybox | uutils) true ;;
    *) false ;;
  esac
}

@test "option permutation matches userland getopt implementation" {
  run_lab "$PROBE" permute "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(userland_flavor) in
    gnu)
      [ "${lines[0]}" = "permuted=yes" ]
      [ "${lines[1]}" = "permuted_posix=no" ]
      ;;
    uutils | busybox)
      # uutils (clap) and BusyBox on musl (Alpine) permute and ignore POSIXLY_CORRECT.
      [ "${lines[0]}" = "permuted=yes" ]
      [ "${lines[1]}" = "permuted_posix=yes" ]
      ;;
    bsd)
      [ "${lines[0]}" = "permuted=no" ]
      [ "${lines[1]}" = "permuted_posix=no" ]
      ;;
    *) false ;;
  esac
}

@test "negative head line count behaves according to userland flavor" {
  run_lab "$PROBE" head_neg "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(userland_flavor) in
    gnu | uutils | busybox)
      [ "${lines[0]}" = "head_neg_status=0" ]
      [ "${lines[1]}" = "head_neg_lines=alpha|bravo|" ]
      [ "${lines[2]}" = "sed_fallback=alpha|bravo|" ]
      ;;
    bsd)
      [ "${lines[0]}" = "head_neg_status=fail" ]
      [ "${lines[1]}" = "sed_fallback=alpha|bravo|" ]
      ;;
    *) false ;;
  esac
}

@test "wc whitespace padding and portable normalization" {
  run_lab "$PROBE" wc_padding "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "wc_raw_trimmed=3" ]
  case $(userland_flavor) in
    bsd)
      [ "${lines[1]}" = "wc_has_padding=yes" ]
      ;;
    gnu | uutils | busybox)
      [ "${lines[1]}" = "wc_has_padding=no" ]
      ;;
    *) false ;;
  esac
  [ "${lines[2]}" = "wc_normalized=3" ]
}

@test "which vs command -v resolution for shell builtins" {
  run_lab "$PROBE" which_builtin "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "which_finds_disk_binary=yes" ]
  [ "${lines[1]}" = "command_v_finds_builtin=yes" ]
}

@test "stat format divergence across userlands" {
  run_lab "$PROBE" stat_format "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(userland_flavor) in
    gnu | uutils | busybox)
      # -f means "file system status" here, so %z is a file operand and the command fails.
      [ "${lines[0]}" = "gnu_stat_size=10" ]
      [ "${lines[1]}" = "bsd_stat_size=fail" ]
      ;;
    bsd)
      [ "${lines[0]}" = "gnu_stat_size=fail" ]
      [ "${lines[1]}" = "bsd_stat_size=10" ]
      ;;
    *) false ;;
  esac
  [ "${lines[2]}" = "posix_wc_size=10" ]
}

@test "tr literal bracket trap and POSIX class equivalence" {
  run_lab "$PROBE" tr_brackets "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "tr_mismatch=AIFMMPZ" ]
  [ "${lines[1]}" = "tr_range=[HELLO]" ]
  [ "${lines[2]}" = "tr_posix=[HELLO]" ]
}

@test "cut multi-character delimiter is rejected except by BusyBox" {
  run_lab "$PROBE" cut_delim "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(userland_flavor) in
    gnu | uutils | bsd)
      [ "${lines[0]}" = "cut_multichar_status=fail" ]
      [ "${lines[1]}" = "awk_split=a" ]
      ;;
    busybox)
      [ "${lines[0]}" = "cut_multichar_status=0" ]
      [ "${lines[1]}" = "cut_multichar_out=a" ]
      [ "${lines[2]}" = "awk_split=a" ]
      ;;
    *) false ;;
  esac
}

@test "tail -r exists only on BSD and awk reverses lines everywhere" {
  run_lab "$PROBE" tail_r "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  case $(userland_flavor) in
    gnu | uutils | busybox)
      [ "${lines[0]}" = "tail_r_status=fail" ]
      [ "${lines[1]}" = "awk_reverse=charlie|bravo|alpha|" ]
      ;;
    bsd)
      [ "${lines[0]}" = "tail_r_status=0" ]
      [ "${lines[1]}" = "tail_r_lines=charlie|bravo|alpha|" ]
      [ "${lines[2]}" = "awk_reverse=charlie|bravo|alpha|" ]
      ;;
    *) false ;;
  esac
}

@test "df -P guarantees standard POSIX header and 6-column record" {
  run_lab "$PROBE" df_portable "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "df_header_ok=yes" ]
  [ "${lines[1]}" = "df_col_count=6" ]
}

@test "double dash prevents option injection on dash-prefixed files" {
  run_lab "$PROBE" arg_injection "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "safe_cat=secret_payload" ]
}
