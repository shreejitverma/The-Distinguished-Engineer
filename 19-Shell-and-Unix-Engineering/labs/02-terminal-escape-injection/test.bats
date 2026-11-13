#!/usr/bin/env bats
# Terminal escape injection: untrusted bytes written to a terminal are code, and
# a terminal reply re-enters the program's input.
#   sanitize.sh drop         strips the dangerous control bytes at the boundary.
#   sanitize.sh payload NAME  builds a known hostile payload.
#   inject_probe term|term2  plays a hostile terminal on a real pty and shows a
#                            reply becoming a completed input line in the shell
#                            under test (its read builtin), with no keystroke.
#   inject_probe show        runs a tool on a pty and prints what it wrote, so the
#                            lab can compare ls on a terminal with ls on a pipe.
#
# sanitize.sh and query-client.sh run under the shell under test (LAB_SHELL).
# ls, cat and tr are utilities: the shell never changes which one runs, so those
# tests branch on the implementation (GNU, BSD, BusyBox, uutils), not the shell.

setup() {
  load ../lib/lab
  SANI="$BATS_TEST_DIRNAME/sanitize.sh"
  CLIENT="$BATS_TEST_DIRNAME/query-client.sh"
  PROBE="$BATS_TEST_TMPDIR/inject_probe"
  lab_shell_words
  # A caller's ls settings must not change what the ls tests see.
  unset QUOTING_STYLE CLICOLOR CLICOLOR_FORCE LS_COLORS
  cd "$BATS_TEST_TMPDIR" || return 1
}

# build_probe: compile the pty harness; a missing compiler fails the test.
build_probe() {
  "${CC:-cc}" -Wall -Wextra -Werror -pedantic -o "$PROBE" "$BATS_TEST_DIRNAME/inject_probe.c" || return 1
}

# sani ARG...: run sanitize.sh under the shell under test, not through its #! line.
sani() {
  "${LAB_SHELL_WORDS[@]}" "$SANI" "$@"
}

# hexfile FILE: print FILE as space-free lowercase hex, so byte assertions are exact.
# od -v, or od folds repeated lines into "*".
hexfile() {
  local dump
  dump=$(od -An -v -tx1 "$1") || return 1
  printf '%s' "$dump" | tr -d ' \n'
}

# all_bytes FILE: write the 256 byte values 0x00 to 0xff to FILE, in order.
all_bytes() {
  local i oct
  : >"$1"
  for ((i = 0; i < 256; i++)); do
    printf -v oct '%03o' "$i"
    # shellcheck disable=SC2059 # the format is the octal escape itself, built from a number
    printf "\\$oct" >>"$1"
  done
}

# impls TOOL: set IMPLS to each installed implementation of TOOL as a command line: TOOL
# itself and, where a busybox binary exists, "busybox TOOL" (on Alpine both name one binary).
impls() {
  IMPLS=("$1")
  if command -v busybox >/dev/null 2>&1; then
    IMPLS+=("busybox $1")
  fi
}

# ls_flavor CMD...: print which ls CMD is (gnu, uutils, busybox, bsd), from what it says
# about itself; BSD and macOS ls reject --version with a usage line.
ls_flavor() {
  local out
  out=$("$@" --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"usage: ls"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# make_hostile_dir: create ./hostile holding one file whose name carries an OSC 2
# set-title sequence: a ESC ] 2 ; x BEL b.
make_hostile_dir() {
  local name
  name=$(printf 'a\033]2;x\007b')
  mkdir hostile || return 1
  : >"hostile/$name"
}

@test "drop strips C0 controls and DEL but keeps TAB and LF" {
  # A ESC B BEL C CR D TAB E LF F  ->  drop keeps only the printable text, TAB and LF.
  printf 'A\033B\007C\rD\tE\nF' >in.bin
  sani drop <in.bin >out.bin
  hex=$(hexfile out.bin)
  # A B C D TAB E LF F = 41 42 43 44 09 45 0a 46
  [ "$hex" = "4142434409450a46" ]
}

@test "drop deletes exactly 31 byte values: C0 (NUL included) except TAB and LF, plus DEL" {
  all_bytes all.bin
  size=$(wc -c <all.bin)
  [ "$size" -eq 256 ]
  # What must survive: TAB, LF, 0x20 to 0x7e, and 0x80 to 0xff (225 values, in order).
  want=""
  for ((i = 0; i < 256; i++)); do
    if [ "$i" -eq 9 ] || [ "$i" -eq 10 ] || { [ "$i" -ge 32 ] && [ "$i" -ne 127 ]; }; then
      printf -v byte '%02x' "$i"
      want+=$byte
    fi
  done
  [ "${#want}" -eq 450 ]
  sani drop <all.bin >kept.bin
  hex=$(hexfile kept.bin)
  [ "$hex" = "$want" ]
  # The same delete set means the same thing to every tr installed here.
  impls tr
  for line in "${IMPLS[@]}"; do
    IFS=' ' read -r -a cmd <<<"$line"
    LC_ALL=C "${cmd[@]}" -d '\000-\010\013-\037\177' <all.bin >kept.bin
    hex=$(hexfile kept.bin)
    echo "impl=$line"
    [ "$hex" = "$want" ] || return 1
  done
}

@test "drop neutralizes a title-report injection payload into inert printable text" {
  sani payload title >title.bin
  sani drop <title.bin >clean.bin
  out=$(cat clean.bin)
  # The ESC and BEL are gone; the structure renders as literal characters.
  [ "$out" = "]2;id[21t" ]
  # ] 2 ; i d [ 2 1 t and nothing else: no ESC (1b), BEL (07) or CR (0d) byte survives.
  hex=$(hexfile clean.bin)
  [ "$hex" = "5d323b69645b323174" ]
}

@test "drop neutralizes the OSC 52 clipboard-read and DECRQSS payloads" {
  sani payload osc52 >osc52.bin
  sani drop <osc52.bin >clean.bin
  out=$(cat clean.bin)
  [ "$out" = "]52;c;?" ]
  sani payload decrqss >decrqss.bin
  sani drop <decrqss.bin >clean.bin
  out=$(cat clean.bin)
  # Both ESC bytes go; the printable remainder P $ q m and the ST's trailing
  # backslash (0x5c is not a control byte) stay as inert text.
  exp=$(printf 'P%sqm\134' '$')
  [ "$out" = "$exp" ]
}

@test "the title payload really does carry ESC and BEL before sanitizing" {
  # Discriminating: proves the payload is dangerous, so drop has something to remove.
  sani payload title >title.bin
  hex=$(hexfile title.bin)
  # ESC ] 2 ; i d BEL ESC [ 2 1 t = 1b 5d 32 3b 69 64 07 1b 5b 32 31 74
  [ "$hex" = "1b5d323b6964071b5b323174" ]
}

@test "tr -d '[:cntrl:]' over-deletes: it also removes TAB and LF, which is why drop names the bytes" {
  # Utility behavior, the same in every tr installed here (GNU, BSD, BusyBox); not shell dependent.
  printf 'A\tB\nC\033D' >in.bin
  impls tr
  for line in "${IMPLS[@]}"; do
    IFS=' ' read -r -a cmd <<<"$line"
    LC_ALL=C "${cmd[@]}" -d '[:cntrl:]' <in.bin >out.bin
    hex=$(hexfile out.bin)
    echo "impl=$line hex=$hex"
    # TAB, LF and ESC all gone, so the pieces join: A B C D.
    [ "$hex" = "41424344" ] || return 1
  done
}

@test "ls to a pipe: GNU, BSD and uutils write a hostile name raw; BusyBox still writes ?" {
  make_hostile_dir
  impls ls
  for line in "${IMPLS[@]}"; do
    IFS=' ' read -r -a cmd <<<"$line"
    flavor=$(ls_flavor "${cmd[@]}")
    LC_ALL=C "${cmd[@]}" hostile | cat >piped.bin
    hex=$(hexfile piped.bin)
    echo "impl=$line flavor=$flavor hex=$hex"
    case $flavor in
      # a ESC ] 2 ; x BEL b LF: the sequence is live in the pipe.
      gnu | uutils | bsd) [ "$hex" = "611b5d323b7807620a" ] || return 1 ;;
      # a ? ] 2 ; x ? b LF: print_name() replaces control bytes whatever stdout is.
      busybox) [ "$hex" = "613f5d323b783f620a" ] || return 1 ;;
      *) return 1 ;;
    esac
  done
}

@test "ls on a terminal: every implementation defuses the name, each in its own rendering" {
  build_probe
  make_hostile_dir
  impls ls
  # GNU and uutils quote in shell-escape style: 'a'$'\033'']2;x'$'\a''b'
  gnu_want="transcript='a'\$'\\033'']2;x'\$'\\a''b'\\r\\n"
  qmark_want='transcript=a?]2;x?b\r\n'
  sgr_on='<ESC>[0;0m'
  sgr_off='<ESC>[m'
  for line in "${IMPLS[@]}"; do
    IFS=' ' read -r -a cmd <<<"$line"
    flavor=$(ls_flavor "${cmd[@]}")
    run env LC_ALL=C "$PROBE" show "${cmd[@]}" hostile
    echo "impl=$line flavor=$flavor output=$output"
    [ "$status" -eq 0 ] || return 1
    [ "${lines[1]}" = "child_status=0" ] || return 1
    shown=${lines[0]}
    case $flavor in
      gnu | uutils) [ "$shown" = "$gnu_want" ] || return 1 ;;
      bsd) [ "$shown" = "$qmark_want" ] || return 1 ;;
      busybox)
        # A BusyBox built with FEATURE_LS_COLOR_IS_DEFAULT (Alpine's) wraps the name in its
        # own SGR pair on a terminal and Debian's build does not; without that wrapper the
        # name is the same ? form in both.
        shown=${shown/"$sgr_on"/}
        shown=${shown/"$sgr_off"/}
        [ "$shown" = "$qmark_want" ] || return 1
        ;;
      *) return 1 ;;
    esac
  done
}

@test "ls -q and ls -b defuse the name on a pipe in GNU, BSD and uutils; BusyBox has neither option" {
  make_hostile_dir
  impls ls
  for line in "${IMPLS[@]}"; do
    IFS=' ' read -r -a cmd <<<"$line"
    flavor=$(ls_flavor "${cmd[@]}")
    echo "impl=$line flavor=$flavor"
    case $flavor in
      gnu | uutils | bsd)
        LC_ALL=C "${cmd[@]}" -q hostile | cat >q.bin
        LC_ALL=C "${cmd[@]}" -b hostile | cat >b.bin
        hex_q=$(hexfile q.bin)
        hex_b=$(hexfile b.bin)
        # -q: a ? ] 2 ; x ? b LF
        [ "$hex_q" = "613f5d323b783f620a" ] || return 1
        # -b: a \ 0 3 3 ] 2 ; x \ a b LF, C escapes spelled in printable ASCII
        [ "$hex_b" = "615c3033335d323b785c61620a" ] || return 1
        ;;
      busybox)
        for opt in q b; do
          run env LC_ALL=C "${cmd[@]}" "-$opt" hostile
          echo "opt=$opt status=$status first=${lines[0]}"
          [ "$status" -eq 1 ] || return 1
          case ${lines[0]} in
            *option*"$opt"*) ;;
            *) return 1 ;;
          esac
        done
        ;;
      *) return 1 ;;
    esac
  done
}

@test "cat copies ESC raw and cat -v renders it as ^[, in every cat installed here" {
  printf 'a\033b\n' >esc.bin
  impls cat
  for line in "${IMPLS[@]}"; do
    IFS=' ' read -r -a cmd <<<"$line"
    "${cmd[@]}" esc.bin >raw.bin
    hex=$(hexfile raw.bin)
    echo "impl=$line raw=$hex"
    [ "$hex" = "611b620a" ] || return 1
    "${cmd[@]}" -v esc.bin >shown.bin
    hex=$(hexfile shown.bin)
    echo "impl=$line shown=$hex"
    # a ^ [ b LF
    [ "$hex" = "615e5b620a" ] || return 1
  done
}

@test "a terminal reply ending in CR becomes a completed input line with no keystroke" {
  build_probe
  run "$PROBE" term "${LAB_SHELL_WORDS[@]}" "$CLIENT" 1
  [ "$status" -eq 0 ]
  # The child asked for its title (ESC [ 21 t); the hostile terminal injected
  # h4ck<CR>; ICRNL turned CR into NL, so the shell's read returned a full line.
  case $output in
    *'<ESC>[21t'*'INPUT=[h4ck] rc=0'*'child_status=0'*) ;;
    *) false ;;
  esac
}

@test "one reply can inject several commands: two CR-terminated lines queue as two reads" {
  build_probe
  run "$PROBE" term2 "${LAB_SHELL_WORDS[@]}" "$CLIENT" 2
  [ "$status" -eq 0 ]
  case $output in
    *'INPUT=[a] rc=0'*'INPUT=[b] rc=0'*'child_status=0'*) ;;
    *) false ;;
  esac
}

@test "the injection path is identical in every shell family under test" {
  # The line discipline, not the shell, assembles the line; assert the per-family
  # result is the same completed line, with an explicit arm so a new family cannot
  # pass silently.
  build_probe
  run "$PROBE" term "${LAB_SHELL_WORDS[@]}" "$CLIENT" 1
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      case $output in
        *'INPUT=[h4ck] rc=0'*) ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}
