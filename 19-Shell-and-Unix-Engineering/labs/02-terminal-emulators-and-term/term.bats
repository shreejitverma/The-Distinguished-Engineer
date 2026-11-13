#!/usr/bin/env bats
# Terminal emulators and TERM.
# miniterm.c is a terminal emulator with no screen: it owns the master side of a pseudo-terminal,
# stores the window size in the kernel, composes the child's whole environment, and answers two
# control sequences. The shell under test (LAB_SHELL) runs inside it, either on term-client.sh or
# as an interactive shell, so every test sees what a program on a real terminal sees.

setup() {
  load ../lib/lab
  CLIENT="$BATS_TEST_DIRNAME/term-client.sh"
  MINITERM="$BATS_TEST_TMPDIR/miniterm"
  EMPTY_HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$EMPTY_HOME"
  # A missing compiler fails every test here; none is skipped.
  "${CC:-cc}" -Wall -Wextra -Werror -pedantic -o "$MINITERM" "$BATS_TEST_DIRNAME/miniterm.c"
  lab_shell_words
  cd "$BATS_TEST_TMPDIR" || return 1
}

# client ROWS COLS [NAME=VALUE...] -- MODE [ARG...]: run term-client.sh MODE under the shell under
# test on a miniterm terminal of ROWS by COLS whose environment is PATH plus the NAME=VALUE words.
client() {
  local -a head=()
  while [ "$1" != "--" ]; do
    head+=("$1")
    shift
    [ "$#" -gt 0 ] || return 1
  done
  shift
  run "$MINITERM" "${head[@]}" "PATH=$PATH" -- "${LAB_SHELL_WORDS[@]}" "$CLIENT" "$@"
}

# interactive TERM_VALUE: start the shell under test as an interactive shell on a 24 by 80 miniterm
# terminal with TERM set to TERM_VALUE, no startup files and an empty HOME, and type "exit" at it.
interactive() {
  local -a flags
  case $(lab_shell_family) in
    bash) flags=(--norc --noprofile -i) ;;
    zsh | zsh-sh) flags=(-f -i) ;;
    yash) flags=(--norcfile -i) ;;
    dash | posh | mksh | ksh93 | busybox) flags=(-i) ;;
    *) return 1 ;;
  esac
  run "$MINITERM" -t exit 24 80 "TERM=$1" "PATH=$PATH" "HOME=$EMPTY_HOME" -- "${LAB_SHELL_WORDS[@]}" "${flags[@]}"
  [ "${#lines[@]}" -eq 2 ]
  [ "${lines[1]}" = "status=0" ]
}

# screen_has TEXT: succeed when the screen transcript in $output contains TEXT.
screen_has() {
  case $output in
    *"$1"*) return 0 ;;
    *) return 1 ;;
  esac
}

# stty_flavor: print the implementation behind stty: gnu, busybox, uutils, or bsd (macOS and the BSDs).
stty_flavor() {
  local out
  out="$(stty --version 2>&1 </dev/null) $(stty --help 2>&1 </dev/null)" || true
  case $out in
    *"GNU coreutils"*) printf 'gnu\n' ;;
    *BusyBox*) printf 'busybox\n' ;;
    *uutils*) printf 'uutils\n' ;;
    *) printf 'bsd\n' ;;
  esac
}

# winsize_policy: print what the shell under test does with COLUMNS and LINES in a script when the
# window changes:
#   tracks      it rewrites both, whether it set them itself or inherited them
#   tracks-own  it rewrites the ones it set itself and leaves an inherited one alone
#   never       it never sets or rewrites either
# An unknown shell prints nothing and fails.
winsize_policy() {
  local version probe
  version=$(lab_shell_version)
  case $(lab_shell_family) in
    bash)
      # checkwinsize: on by default since bash 5.0, and honoured by non-interactive shells since 4.3.
      case $version in
        [0-4].*) printf 'never\n' ;;
        5.*) printf 'tracks\n' ;;
        *) return 1 ;;
      esac
      ;;
    zsh | zsh-sh) printf 'tracks\n' ;;
    ksh93)
      # ksh93u+m installs a SIGWINCH handler in scripts; ksh93u+ 2012 (macOS /bin/ksh) does not.
      case $version in
        *93u+m*) printf 'tracks\n' ;;
        *93u+*) printf 'never\n' ;;
        *) return 1 ;;
      esac
      ;;
    mksh)
      # mksh leaves an imported COLUMNS or LINES alone ("do not touch" in setspec, var.c) for as
      # long as the variable carries its IMPORT flag. R59c (R59 2020/10/31, the Alpine build) keeps
      # the flag for the life of the shell. CVS since 2021-05-02 (Debian's R59 2025/04/26) clears it
      # when startup turns the variable into an integer; the same commit stops an imported "1+1"
      # from being evaluated as arithmetic, which is the behavior probed here.
      # shellcheck disable=SC2016 # the expansion belongs to the shell under test
      probe=$(LAB_PROBE=1+1 "${LAB_SHELL_WORDS[@]}" -c 'typeset -i LAB_PROBE; print -r -- "$LAB_PROBE"') || return 1
      case $probe in
        2) printf 'tracks-own\n' ;;
        0) printf 'tracks\n' ;;
        *) return 1 ;;
      esac
      ;;
    dash | posh | busybox | yash) printf 'never\n' ;;
    *) return 1 ;;
  esac
}

@test "TERM is whatever the emulator exported, byte for byte, and a child process inherits it" {
  client 24 80 TERM=lab-9000 -- env
  [ "${lines[0]}" = 'screen=shell=lab-9000 child=lab-9000\r\n' ]
  [ "${lines[1]}" = "status=0" ]
  # The value is an opaque string: POSIX leaves its format unspecified, so spaces survive too.
  client 24 80 "TERM=two words" -- env
  [ "${lines[0]}" = 'screen=shell=two words child=two words\r\n' ]
  [ "${lines[1]}" = "status=0" ]
}

@test "with no TERM in the environment only bash invents one, and it does not export it" {
  local -a no_site_files=()
  case $(lab_shell_family) in
    # zsh reads the site zshenv even for a script, and a site file may assign TERM (nix-darwin's
    # runs "export TERM=$TERM", which turns an unset TERM into an exported empty one), so native
    # zsh runs with -f (NO_RCS) here to show the shell's own behavior.
    zsh) no_site_files=(-f) ;;
    bash | zsh-sh | dash | posh | mksh | ksh93 | yash | busybox) ;;
    *) false ;;
  esac
  run "$MINITERM" 24 80 "PATH=$PATH" -- "${LAB_SHELL_WORDS[@]}" "${no_site_files[@]}" "$CLIENT" env
  [ "${lines[1]}" = "status=0" ]
  case $(lab_shell_family) in
    bash) [ "${lines[0]}" = 'screen=shell=dumb child=unset\r\n' ] ;;
    zsh | zsh-sh | dash | posh | mksh | ksh93 | yash | busybox)
      [ "${lines[0]}" = 'screen=shell=unset child=unset\r\n' ]
      ;;
    *) false ;;
  esac
}

@test "the window size is the kernel's record of what the emulator stored, whatever TERM says" {
  # The vt100 terminfo entry describes a 24 by 80 screen; the kernel holds what miniterm set.
  client 50 132 TERM=vt100 -- size
  [ "${lines[1]}" = "status=0" ]
  screen_has 'kernel: 50 132\r\n'
  client 24 80 TERM=vt100 -- size
  [ "${lines[1]}" = "status=0" ]
  screen_has 'kernel: 24 80\r\n'
}

@test "COLUMNS and LINES in a script: zsh, mksh and ksh93u+m set them at startup, bash 5 after a foreground command, the rest never" {
  local unset_both='start: COLUMNS=unset LINES=unset\r\nkernel: 50 132\r\nafter: COLUMNS=unset LINES=unset\r\n'
  local set_both='start: COLUMNS=132 LINES=50\r\nkernel: 50 132\r\nafter: COLUMNS=132 LINES=50\r\n'
  local set_late='start: COLUMNS=unset LINES=unset\r\nkernel: 50 132\r\nafter: COLUMNS=132 LINES=50\r\n'
  client 50 132 TERM=vt100 -- size
  [ "${lines[1]}" = "status=0" ]
  case $(lab_shell_family) in
    zsh | zsh-sh | mksh) [ "${lines[0]}" = "screen=$set_both" ] ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) [ "${lines[0]}" = "screen=$set_both" ] ;;
        *93u+*) [ "${lines[0]}" = "screen=$unset_both" ] ;;
        *) false ;;
      esac
      ;;
    bash)
      case $(lab_shell_version) in
        [0-4].*) [ "${lines[0]}" = "screen=$unset_both" ] ;;
        5.*) [ "${lines[0]}" = "screen=$set_late" ] ;;
        *) false ;;
      esac
      ;;
    dash | posh | busybox | yash) [ "${lines[0]}" = "screen=$unset_both" ] ;;
    *) false ;;
  esac
}

@test "a resize request changes the kernel's size, and the program sees it after a Device Attributes round trip" {
  client 24 80 TERM=vt100 -- resize 40 132
  [ "${lines[1]}" = "status=0" ]
  screen_has 'before: kernel=24 80 '
  screen_has '<ESC>[8;40;132t<ESC>[c'
  screen_has 'after: kernel=40 132 '
  # A different request gives a different size, so the "after" line is not a constant.
  client 24 80 TERM=vt100 -- resize 10 30
  screen_has 'after: kernel=10 30 '
}

@test "an inherited COLUMNS goes stale after a resize in the shells that do not rewrite it, and old mksh freezes only what it inherited" {
  local policy
  policy=$(winsize_policy)
  client 24 80 TERM=vt100 COLUMNS=80 LINES=24 -- resize 40 132
  [ "${lines[1]}" = "status=0" ]
  screen_has 'before: kernel=24 80 COLUMNS=80 LINES=24\r\n'
  case $policy in
    tracks) screen_has 'after: kernel=40 132 COLUMNS=132 LINES=40 stty=40 132\r\n' ;;
    tracks-own | never)
      # The shell still says 80 by 24, and it hands the stale COLUMNS and LINES on to its children.
      case $(stty_flavor) in
        gnu | bsd) screen_has 'after: kernel=40 132 COLUMNS=80 LINES=24 stty=40 132\r\n' ;;
        busybox) screen_has 'after: kernel=40 132 COLUMNS=80 LINES=24 stty=24 80\r\n' ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
  # Only LINES is inherited now, so COLUMNS shows what the shell does with a variable of its own.
  client 24 80 TERM=vt100 LINES=24 -- resize 40 132
  [ "${lines[1]}" = "status=0" ]
  screen_has 'before: kernel=24 80 '
  case $policy in
    tracks) screen_has 'after: kernel=40 132 COLUMNS=132 LINES=40 ' ;;
    tracks-own) screen_has 'after: kernel=40 132 COLUMNS=132 LINES=24 ' ;;
    never) screen_has 'after: kernel=40 132 COLUMNS=unset LINES=24 ' ;;
    *) false ;;
  esac
}

@test "stty size: BusyBox lets LINES and COLUMNS override the kernel, GNU and BSD stty do not" {
  run "$MINITERM" 24 80 "PATH=$PATH" LINES=17 COLUMNS=91 -- stty size
  [ "${lines[1]}" = "status=0" ]
  case $(stty_flavor) in
    gnu | bsd) [ "${lines[0]}" = 'screen=24 80\r\n' ] ;;
    busybox) [ "${lines[0]}" = 'screen=17 91\r\n' ] ;;
    *) false ;;
  esac
}

@test "the terminal answers Device Attributes with its own identity, whatever TERM claims" {
  client 24 80 TERM=vt100 -- identify
  [ "${lines[0]}" = 'screen=<ESC>[cTERM=vt100\r\nreply=ESC[?62;22c\r\n' ]
  [ "${lines[1]}" = "status=0" ]
  client 24 80 TERM=xterm-256color -- identify
  [ "${lines[0]}" = 'screen=<ESC>[cTERM=xterm-256color\r\nreply=ESC[?62;22c\r\n' ]
  [ "${lines[1]}" = "status=0" ]
}

@test "a terminal that never answers costs the whole timeout and yields no reply" {
  local start=$SECONDS
  client -m 24 80 TERM=xterm-256color -- identify
  local elapsed=$((SECONDS - start))
  [ "${lines[0]}" = 'screen=<ESC>[cTERM=xterm-256color\r\nreply=none\r\n' ]
  [ "${lines[1]}" = "status=0" ]
  # The client waits VTIME 20 (2 seconds) for the first byte; at least 1 second must have passed.
  [ "$elapsed" -ge 1 ]
}

@test "bracketed paste: bash 5.1 and later send it only for a TERM they know, zsh for any TERM, the other shells never" {
  local term seen=""
  for term in dumb xterm nosuch-term-9; do
    interactive "$term"
    if screen_has '<ESC>[?2004h'; then
      seen="$seen$term=yes "
    else
      seen="$seen$term=no "
    fi
  done
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        [0-4].* | 5.0.*) [ "$seen" = "dumb=no xterm=no nosuch-term-9=no " ] ;;
        5.[1-9]*) [ "$seen" = "dumb=no xterm=yes nosuch-term-9=no " ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh) [ "$seen" = "dumb=yes xterm=yes nosuch-term-9=yes " ] ;;
    dash | posh | mksh | ksh93 | yash | busybox) [ "$seen" = "dumb=no xterm=no nosuch-term-9=no " ] ;;
    *) false ;;
  esac
}

@test "TERM=dumb or an unknown TERM silences bash, yash, ksh93 and mksh, but zsh and BusyBox ash still write escape sequences" {
  local term seen=""
  for term in dumb nosuch-term-9; do
    interactive "$term"
    if screen_has '<ESC>'; then
      seen="$seen$term=yes "
    else
      seen="$seen$term=no "
    fi
  done
  case $(lab_shell_family) in
    # zsh sends bracketed paste on and off; BusyBox ash asks for the cursor position with ESC [ 6 n
    # (its FEATURE_EDITING_ASK_TERMINAL, enabled in the Alpine and Debian builds of 1.37.0).
    zsh | zsh-sh | busybox) [ "$seen" = "dumb=yes nosuch-term-9=yes " ] ;;
    bash | dash | posh | mksh | ksh93 | yash) [ "$seen" = "dumb=no nosuch-term-9=no " ] ;;
    *) false ;;
  esac
}
