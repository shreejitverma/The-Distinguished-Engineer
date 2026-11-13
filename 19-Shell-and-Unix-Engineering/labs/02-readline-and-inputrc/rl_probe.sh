#!/bin/sh
# rl_probe.sh: start SHELL interactively on a pseudo-terminal with a controlled HOME, TERM and
# INPUTRC, type at it, and report what its line editor did.
# Usage: rl_probe.sh CASE SHELL [ARG...]
# PROBE names the compiled rl_pty_probe; the scratch HOME is made under BATS_TEST_TMPDIR.
# Every marker is the result of an arithmetic expansion ($((6*7)) -> 42), so it appears only
# when the shell executed the command, never in the echo of the typed or bound text.
# Example: PROBE=./rl_pty_probe rl_probe.sh macro-env bash  ->  ready=1 M42F=1 alive=1

set -e

if [ "$#" -lt 2 ]; then
  printf 'usage: %s CASE SHELL [ARG...]\n' "$0" >&2
  exit 2
fi
mode=$1
shift

home=$(mktemp -d "${BATS_TEST_TMPDIR:-${PWD}}/home.XXXXXX")
# An empty .zshrc keeps zsh from starting its new-user configuration dialog.
: >"${home}/.zshrc"

# One macro on C-x q: the text is typed for the user, and the trailing \n accepts the line.
write_macro() {
  cat >"$1" <<'EOF'
"\C-xq": "echo M$((6*7))F\n"
EOF
}

# shellcheck disable=SC2016 # the typed keys keep $((...)) literal: the shell on the terminal expands it
case ${mode} in
  macro-env)
    # INPUTRC names the file; there is no ~/.inputrc.
    write_macro "${home}/rc"
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC="${home}/rc" \
      "${PROBE}" keys 'k:\x18q' m:M42F -- "$@"
    ;;
  macro-home)
    # INPUTRC is unset, so ~/.inputrc is the init file.
    write_macro "${home}/.inputrc"
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm \
      "${PROBE}" keys 'k:\x18q' m:M42F -- "$@"
    ;;
  macro-env-wins)
    # Both exist: INPUTRC binds C-x q, ~/.inputrc binds C-x w. Only one file is read.
    write_macro "${home}/rc"
    cat >"${home}/.inputrc" <<'EOF'
"\C-xw": "echo H$((6*7))F\n"
EOF
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC="${home}/rc" \
      "${PROBE}" keys 'k:\x18q' 'k:\x18w' m:M42F m:H42F -- "$@"
    ;;
  macro-env-missing)
    # INPUTRC names a file that does not exist: there is no fallback to ~/.inputrc.
    write_macro "${home}/.inputrc"
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC="${home}/absent" \
      "${PROBE}" keys 'k:\x18q' m:M42F -- "$@"
    ;;
  macro-include)
    # The init file holds one $include line; the macro lives in the file it names.
    write_macro "${home}/more.rc"
    printf '$include %s\n' "${home}/more.rc" >"${home}/rc"
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC="${home}/rc" \
      "${PROBE}" keys 'k:\x18q' m:M42F -- "$@"
    ;;
  conditionals)
    # Each $if binds its own key in both branches, so the output names the branch taken.
    cat >"${home}/rc" <<'EOF'
$if Bash
"\C-xa": "echo IF$((1+1))bash\n"
$else
"\C-xa": "echo IF$((1+1))other\n"
$endif
$if mode=vi
"\C-xb": "echo MODE$((1+1))vi\n"
$else
"\C-xb": "echo MODE$((1+1))emacs\n"
$endif
$if term=xterm
"\C-xc": "echo TERM$((1+1))xterm\n"
$else
"\C-xc": "echo TERM$((1+1))other\n"
$endif
$if version >= 8.0
"\C-xd": "echo VER$((1+1))new\n"
$else
"\C-xd": "echo VER$((1+1))old\n"
$endif
EOF
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC="${home}/rc" \
      "${PROBE}" keys 'k:\x18a' 'k:\x18b' 'k:\x18c' 'k:\x18d' \
      m:IF2bash m:IF2other m:MODE2emacs m:MODE2vi m:TERM2xterm m:TERM2other m:VER2new m:VER2old -- "$@"
    ;;
  emacs-default)
    # No init file and no option: C-a then C-k empties the line only in an emacs-style editor.
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC=/dev/null \
      "${PROBE}" keys 'k:XXjunk\x01\x0b' 'k:echo D$((2+3))E\r' m:D5E -- "$@"
    ;;
  vi-default)
    # No init file and no option: is the default editor a vi-mode editor?
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC=/dev/null \
      "${PROBE}" keys 'k:XXecho V$((2+3))I' 'k:\e' 'k:0xx\r' m:V5I -- "$@"
    ;;
  vi-set-o)
    # The POSIX way: set -o vi, typed at the prompt.
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC=/dev/null \
      "${PROBE}" keys 'k:set -o vi\r' 'k:XXecho V$((2+3))I' 'k:\e' 'k:0xx\r' m:V5I -- "$@"
    ;;
  vi-inputrc)
    # The Readline way: set editing-mode vi in the init file.
    printf 'set editing-mode vi\n' >"${home}/rc"
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC="${home}/rc" \
      "${PROBE}" keys 'k:XXecho V$((2+3))I' 'k:\e' 'k:0xx\r' m:V5I -- "$@"
    ;;
  termios)
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC=/dev/null \
      "${PROBE}" termios -- "$@"
    ;;
  paste-on | paste-off)
    # The init file sets the variable explicitly.
    printf 'set enable-bracketed-paste %s\n' "${mode#paste-}" >"${home}/rc"
    exec env -i PATH="${PATH}" HOME="${home}" TERM=xterm INPUTRC="${home}/rc" \
      "${PROBE}" paste -- "$@"
    ;;
  paste-default | paste-dumb)
    # No init file: the editor's own default, on a terminal named xterm and on one named dumb.
    term=xterm
    if [ "${mode}" = paste-dumb ]; then
      term=dumb
    fi
    exec env -i PATH="${PATH}" HOME="${home}" TERM="${term}" INPUTRC=/dev/null \
      "${PROBE}" paste -- "$@"
    ;;
  *)
    printf '%s: unknown case %s\n' "$0" "${mode}" >&2
    exit 2
    ;;
esac
