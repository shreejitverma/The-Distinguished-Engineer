#!/usr/bin/env bats

# The shell under test never changes which uniq runs, so utility behavior is asserted per
# implementation (uniq_flavor), looping over every uniq installed here (uniq_impls).

setup() {
  load ../lib/lab
}

# uniq_impls: print each uniq implementation installed here, one command per line.
uniq_impls() {
  local cmd
  for cmd in uniq guniq uu-uniq; do
    if command -v "$cmd" >/dev/null 2>&1; then
      printf '%s\n' "$cmd"
    fi
  done
  if command -v busybox >/dev/null 2>&1 && busybox uniq </dev/null >/dev/null 2>&1; then
    printf '%s\n' "busybox uniq"
  fi
}

# uniq_flavor CMD...: print gnu, uutils, busybox, bsd (FreeBSD and macOS), or unknown.
uniq_flavor() {
  local out
  out="$("$@" --version 2>&1 </dev/null)" || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox* | *"[-cduiz]"*) echo busybox ;;
    *"usage: uniq [-cdiu]"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# uniq_gnu_mb CMD...: succeed if a GNU uniq is 9.5 or later, where -s and -w count characters, not bytes.
uniq_gnu_mb() {
  local banner ver major minor
  banner=$("$@" --version 2>/dev/null </dev/null) || return 1
  ver=$(sed -n '1s/.* \([0-9][0-9]*\)\.\([0-9][0-9]*\).*/\1 \2/p' <<<"$banner")
  read -r major minor <<<"$ver"
  [ "${major:-0}" -gt 9 ] || { [ "${major:-0}" -eq 9 ] && [ "${minor:-0}" -ge 5 ]; }
}

# lab_impls: the implementation list, which must not be empty.
lab_impls() {
  IMPLS=$(uniq_impls)
  [ -n "$IMPLS" ]
}

@test "topk.sh counts non-adjacent duplicates, keeps leading blanks, and breaks ties by line" {
  printf 'b\n  lead\na\nb\n  lead\nc\na\nb\n' >"$BATS_TEST_TMPDIR/in"
  run_lab "$BATS_TEST_DIRNAME/topk.sh" 3 <"$BATS_TEST_TMPDIR/in"
  [ "$status" -eq 0 ]
  [ "${#lines[@]}" -eq 3 ]
  [ "${lines[0]}" = "$(printf '3\tb')" ]
  [ "${lines[1]}" = "$(printf '2\t  lead')" ]
  [ "${lines[2]}" = "$(printf '2\ta')" ]
}

@test "topk.sh rejects a count that is not a decimal integer" {
  run_lab "$BATS_TEST_DIRNAME/topk.sh" 3x </dev/null
  [ "$status" -eq 2 ]
  [ "$output" = 'topk.sh: N must be a decimal integer, got "3x"' ]
}

@test "uniq compares only adjacent lines: an unsorted repeat is a new group" {
  local cmd
  lab_impls
  while IFS= read -r cmd; do
    run_lab_c "printf 'a\\nb\\na\\na\\n' | $cmd -c | sed 's/^ *//'" </dev/null
    echo "impl=$cmd output=$output"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 3 ]
    [ "${lines[0]}" = "1 a" ]
    [ "${lines[1]}" = "1 b" ]
    [ "${lines[2]}" = "2 a" ]
  done <<<"$IMPLS"
}

@test "uniq -c pads the count to 7 columns in GNU, BusyBox, and uutils, 4 in BSD" {
  local cmd flavor
  lab_impls
  while IFS= read -r cmd; do
    # shellcheck disable=SC2086 # split a multi-word command such as "busybox uniq"
    flavor=$(uniq_flavor $cmd)
    run_lab_c "printf 'a\\na\\nb\\n' | $cmd -c | tr ' ' _" </dev/null
    echo "impl=$cmd flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu | busybox | uutils)
        [ "${lines[0]}" = "______2_a" ]
        [ "${lines[1]}" = "______1_b" ]
        ;;
      bsd)
        [ "${lines[0]}" = "___2_a" ]
        [ "${lines[1]}" = "___1_b" ]
        ;;
      *) false ;;
    esac
  done <<<"$IMPLS"
}

@test "an unterminated last line: GNU, BusyBox, and uutils add the newline, BSD copies it as is" {
  local cmd flavor
  lab_impls
  while IFS= read -r cmd; do
    # shellcheck disable=SC2086 # split a multi-word command such as "busybox uniq"
    flavor=$(uniq_flavor $cmd)
    run_lab_c "printf 'a\\na\\nb' | $cmd; printf '|'" </dev/null
    echo "impl=$cmd flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu | busybox | uutils) [ "$output" = "$(printf 'a\nb\n|')" ] ;;
      bsd) [ "$output" = "$(printf 'a\nb|')" ] ;;
      *) false ;;
    esac
  done <<<"$IMPLS"
}

@test "the trailing newline is not part of the comparison: 'a' and 'a<newline>' are one group" {
  local cmd
  lab_impls
  while IFS= read -r cmd; do
    run_lab_c "printf 'a\\na' | $cmd -c | sed 's/^ *//'" </dev/null
    echo "impl=$cmd output=$output"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
    [ "$output" = "2 a" ]
  done <<<"$IMPLS"
}

@test "-f skips a field and keeps the blanks after it, so 'a  x' and 'b x' differ" {
  local cmd
  lab_impls
  while IFS= read -r cmd; do
    run_lab_c "printf 'a  x\\nb x\\nc x\\n' | $cmd -c -f 1 | sed 's/^ *//'" </dev/null
    echo "impl=$cmd output=$output"
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 2 ]
    [ "${lines[0]}" = "1 a  x" ]
    [ "${lines[1]}" = "2 b x" ]
  done <<<"$IMPLS"
}

@test "-d with -u prints nothing in GNU, BusyBox, and uutils, but the repeated lines in BSD" {
  local cmd flavor
  lab_impls
  while IFS= read -r cmd; do
    # shellcheck disable=SC2086 # split a multi-word command such as "busybox uniq"
    flavor=$(uniq_flavor $cmd)
    run_lab_c "printf 'a\\na\\nb\\n' | $cmd -d -u; printf '|'" </dev/null
    echo "impl=$cmd flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu | busybox | uutils) [ "$output" = "|" ] ;;
      bsd) [ "$output" = "$(printf 'a\n|')" ] ;;
      *) false ;;
    esac
  done <<<"$IMPLS"
}

@test "a NUL byte inside a line: GNU and uutils keep it, BusyBox ends the line there, BSD drops the rest" {
  local cmd flavor
  lab_impls
  while IFS= read -r cmd; do
    # shellcheck disable=SC2086 # split a multi-word command such as "busybox uniq"
    flavor=$(uniq_flavor $cmd)
    run_lab_c "printf 'a\\000x\\na\\000y\\n' | $cmd | tr '\\000' @; printf '|'" </dev/null
    echo "impl=$cmd flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu | uutils) [ "$output" = "$(printf 'a@x\na@y\n|')" ] ;;
      # bb_get_chunk_from_file() stops at a NUL byte, so "a<NUL>x" reads as two lines, "a" and "x".
      busybox) [ "$output" = "$(printf 'a\nx\na\ny\n|')" ] ;;
      # mbstowcs() and fprintf("%s") stop at the NUL: both lines become "a", one group, no newline.
      bsd) [ "$output" = "a|" ] ;;
      *) false ;;
    esac
  done <<<"$IMPLS"
}

@test "-s counts characters in GNU 9.5+ and BSD, bytes in BusyBox, uutils, and older GNU" {
  local cmd flavor want
  lab_impls
  while IFS= read -r cmd; do
    # shellcheck disable=SC2086 # split a multi-word command such as "busybox uniq"
    flavor=$(uniq_flavor $cmd)
    # U+00E9 and U+00E8 share their first UTF-8 byte (0xc3); skipping one character leaves "x" in both.
    run_lab_c "printf '\\303\\251x\\n\\303\\250x\\n' | LC_ALL=C.UTF-8 $cmd -c -s 1 | sed 's/^ *\\([0-9]*\\) .*/\\1/' | tr '\\n' ' '" </dev/null
    echo "impl=$cmd flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu)
        # shellcheck disable=SC2086 # split a multi-word command such as "busybox uniq"
        if uniq_gnu_mb $cmd; then want="2 "; else want="1 1 "; fi
        ;;
      bsd) want="2 " ;;
      busybox | uutils) want="1 1 " ;;
      *) false ;;
    esac
    [ "$output" = "$want" ]
  done <<<"$IMPLS"
}

@test "-i folds only ASCII case in GNU, BusyBox, and uutils; BSD folds U+00C9 to U+00E9" {
  local cmd flavor
  lab_impls
  while IFS= read -r cmd; do
    # shellcheck disable=SC2086 # split a multi-word command such as "busybox uniq"
    flavor=$(uniq_flavor $cmd)
    run_lab_c "printf 'A\\na\\n\\303\\251\\n\\303\\211\\n' | LC_ALL=C.UTF-8 $cmd -c -i | sed 's/^ *\\([0-9]*\\) .*/\\1/' | tr '\\n' ' '" </dev/null
    echo "impl=$cmd flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu | busybox | uutils) [ "$output" = "2 1 1 " ] ;;
      bsd) [ "$output" = "2 2 " ] ;;
      *) false ;;
    esac
  done <<<"$IMPLS"
}

@test "sort | uniq -i merges nothing in the C locale; sort -f | uniq -i merges case variants" {
  local cmd
  lab_impls
  while IFS= read -r cmd; do
    run_lab_c "printf 'b\\nA\\na\\nB\\n' | LC_ALL=C sort | $cmd -c -i | sed 's/^ *//' | tr '\\n' ' '; printf '/'; printf 'b\\nA\\na\\nB\\n' | LC_ALL=C sort -f | $cmd -c -i | sed 's/^ *//' | tr '\\n' ' '" </dev/null
    echo "impl=$cmd output=$output"
    [ "$status" -eq 0 ]
    [ "$output" = "1 A 1 B 1 a 1 b /2 A 2 B " ]
  done <<<"$IMPLS"
}

@test "-z: GNU and uutils keep a newline inside a record, BusyBox still splits on it, BSD rejects -z" {
  local cmd flavor
  lab_impls
  while IFS= read -r cmd; do
    # shellcheck disable=SC2086 # split a multi-word command such as "busybox uniq"
    flavor=$(uniq_flavor $cmd)
    # Two identical NUL-terminated records, each containing a newline.
    run_lab_c "printf 'x\\ny\\000x\\ny\\000' | $cmd -z 2>/dev/null | tr '\\000' @; printf '|'" </dev/null
    echo "impl=$cmd flavor=$flavor output=$output"
    [ "$status" -eq 0 ]
    case $flavor in
      gnu | uutils) [ "$output" = "$(printf 'x\ny@|')" ] ;;
      # BusyBox reads lines that end at a newline or a NUL and -z changes only the output terminator.
      busybox) [ "$output" = "x@y@x@y@|" ] ;;
      bsd) [ "$output" = "|" ] ;;
      *) false ;;
    esac
  done <<<"$IMPLS"
}

@test "-w and -D: which implementations accept them" {
  local cmd flavor w_status d_status
  lab_impls
  while IFS= read -r cmd; do
    # shellcheck disable=SC2086 # split a multi-word command such as "busybox uniq"
    flavor=$(uniq_flavor $cmd)
    run_lab_c "printf 'ab\\nac\\n' | $cmd -w 1" </dev/null
    w_status=$status
    [ "$w_status" -ne 0 ] || [ "$output" = "ab" ]
    run_lab_c "printf 'a\\na\\nb\\n' | $cmd -D" </dev/null
    d_status=$status
    [ "$d_status" -ne 0 ] || [ "$output" = "$(printf 'a\na')" ]
    echo "impl=$cmd flavor=$flavor -w=$w_status -D=$d_status"
    # One assertion per line: a failing test before && would not fail the bats test.
    case $flavor in
      gnu | uutils)
        [ "$w_status" -eq 0 ]
        [ "$d_status" -eq 0 ]
        ;;
      busybox)
        [ "$w_status" -eq 0 ]
        [ "$d_status" -ne 0 ]
        ;;
      bsd)
        [ "$w_status" -ne 0 ]
        [ "$d_status" -eq 0 ]
        ;;
      *) false ;;
    esac
  done <<<"$IMPLS"
}

@test "the second operand is an output file: 'uniq a b' overwrites b instead of reading it" {
  local cmd
  lab_impls
  while IFS= read -r cmd; do
    printf 'x\nx\n' >"$BATS_TEST_TMPDIR/a"
    printf 'precious\n' >"$BATS_TEST_TMPDIR/b"
    run_lab_c "$cmd \"\$1\" \"\$2\" && cat \"\$2\"" sh "$BATS_TEST_TMPDIR/a" "$BATS_TEST_TMPDIR/b" </dev/null
    echo "impl=$cmd output=$output"
    [ "$status" -eq 0 ]
    [ "$output" = "x" ]
  done <<<"$IMPLS"
}

@test "uniq f f truncates f before reading it (POSIX leaves this unspecified)" {
  local cmd size
  lab_impls
  while IFS= read -r cmd; do
    printf 'x\nx\ny\n' >"$BATS_TEST_TMPDIR/f"
    run_lab_c "$cmd \"\$1\" \"\$1\"" sh "$BATS_TEST_TMPDIR/f" </dev/null
    size=$(wc -c <"$BATS_TEST_TMPDIR/f")
    echo "impl=$cmd status=$status size=$size"
    [ "$status" -eq 0 ]
    [ "$size" -eq 0 ]
  done <<<"$IMPLS"
}

@test "read with default IFS strips the leading blanks of a uniq -c line" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'printf "  lead\n  lead\n" | uniq -c | { read -r n line; printf "<%s><%s>\n" "$n" "$line"; }' </dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "<2><lead>" ]
}

@test "wc -l counts newline bytes, so an unterminated last line is not counted; grep -c '' counts it" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'n=$(printf "a\nb" | wc -l | tr -d " "); g=$(printf "a\nb" | grep -c ""); echo "$n $g"' </dev/null
  [ "$status" -eq 0 ]
  [ "$output" = "1 2" ]
}

@test "awk '!seen[\$0]++' removes non-adjacent duplicates and keeps first-seen order in every awk" {
  local awks awk_cmd
  awks=$(lab_awks)
  [ -n "$awks" ]
  while IFS= read -r awk_cmd; do
    run_lab_c "printf 'c\\na\\nc\\nb\\na\\n' | $awk_cmd '!seen[\$0]++' | tr '\\n' ' '" </dev/null
    echo "awk=$awk_cmd output=$output"
    [ "$status" -eq 0 ]
    [ "$output" = "c a b " ]
  done <<<"$awks"
}
