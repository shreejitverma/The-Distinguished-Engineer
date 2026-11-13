#!/usr/bin/env bats
# Tests for Readline and inputrc: every interactive test starts the shell under test on a
# pseudo-terminal (rl_pty_probe) and types at it, because line editing exists only on a terminal.

setup() {
  load ../lib/lab
  SCRIPT="${BATS_TEST_DIRNAME}/rl_probe.sh"
  PROBE="${BATS_TEST_TMPDIR}/rl_pty_probe"
  if [ ! -x "${PROBE}" ]; then
    "${CC:-cc}" -O2 -Wall -Wextra -Werror "${BATS_TEST_DIRNAME}/rl_pty_probe.c" -o "${PROBE}"
  fi
  export PROBE
  lab_shell_words
  FAMILY=$(lab_shell_family)
  VERSION=$(lab_shell_version)
}

# bash_at_least MAJOR MINOR: true when the bash under test is that release or newer.
bash_at_least() {
  local major minor
  major=${VERSION%%.*}
  minor=${VERSION#*.}
  minor=${minor%%.*}
  [ "${major}" -gt "$1" ] || { [ "${major}" -eq "$1" ] && [ "${minor}" -ge "$2" ]; }
}

# bash_version_test_works: true when the bash under test has a correct `$if version` test, which is
# Readline 8.2 (bash 5.2) or bash 5.1 from patch 7 (bash51-007 reversed the inverted comparison).
bash_version_test_works() {
  local patch
  bash_at_least 5 2 && return 0
  bash_at_least 5 1 || return 1
  patch=${VERSION#5.1.}
  patch=${patch%%[!0-9]*}
  [ -n "${patch}" ] && [ "${patch}" -ge 7 ]
}

# dash_has_libedit: dash has an fc builtin only when it was built with libedit (--with-libedit),
# and that build is the only dash with a line editor.
dash_has_libedit() {
  run_lab_c 'command -v fc'
  [ "${status}" -eq 0 ]
}

# busybox_has_vi_editing: BusyBox compiles its vi editing mode in only with FEATURE_EDITING_VI.
# A build with the bbconfig applet (Alpine) prints its own configuration; a build without the
# applet (Debian 1.37.0) is expected to have no vi mode, which is what Debian's build does.
busybox_has_vi_editing() {
  local config
  config=$(busybox bbconfig 2>/dev/null) || return 1
  case ${config} in
    *CONFIG_FEATURE_EDITING_VI=y*) return 0 ;;
    *) return 1 ;;
  esac
}

# probe CASE: run one rl_probe.sh case with the shell under test as the interactive shell.
probe() {
  run_lab "${SCRIPT}" "$1" "${LAB_SHELL_WORDS[@]}"
  [ "${status}" -eq 0 ]
}

@test "a macro bound in the INPUTRC file runs only under a Readline shell" {
  probe macro-env
  case ${FAMILY} in
    bash) [ "${output}" = "ready=1 M42F=1 alive=1" ] ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "${output}" = "ready=1 M42F=0 alive=1" ] ;;
    *) false ;;
  esac
}

@test "with INPUTRC unset Readline reads ~/.inputrc" {
  probe macro-home
  case ${FAMILY} in
    bash) [ "${output}" = "ready=1 M42F=1 alive=1" ] ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "${output}" = "ready=1 M42F=0 alive=1" ] ;;
    *) false ;;
  esac
}

@test "INPUTRC replaces ~/.inputrc: the binding that exists only in ~/.inputrc is never made" {
  probe macro-env-wins
  case ${FAMILY} in
    bash) [ "${output}" = "ready=1 M42F=1 H42F=0 alive=1" ] ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "${output}" = "ready=1 M42F=0 H42F=0 alive=1" ] ;;
    *) false ;;
  esac
}

@test "an INPUTRC that names a missing file does not fall back to ~/.inputrc" {
  # The same ~/.inputrc makes the macro run in the INPUTRC-unset test above.
  probe macro-env-missing
  case ${FAMILY} in
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "${output}" = "ready=1 M42F=0 alive=1" ] ;;
    *) false ;;
  esac
}

@test "\$include reads another init file in place" {
  probe macro-include
  case ${FAMILY} in
    bash) [ "${output}" = "ready=1 M42F=1 alive=1" ] ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "${output}" = "ready=1 M42F=0 alive=1" ] ;;
    *) false ;;
  esac
}

@test "\$if selects by application, editing mode, terminal, and (from Readline 8.2) version" {
  probe conditionals
  case ${FAMILY} in
    bash)
      if bash_version_test_works; then
        [ "${output}" = "ready=1 IF2bash=1 IF2other=0 MODE2emacs=1 MODE2vi=0 TERM2xterm=1 TERM2other=0 VER2new=1 VER2old=0 alive=1" ]
      else
        # Readline before 8.0 (bash 4.4 and older) reads "version" as an application name, so
        # the test is false and the $else branch binds. Readline 8.0 (bash 5.0) parses the
        # comparison but inverts its result, so "version >= 8.0" is false there too; Readline 8.1
        # and bash 5.1.0 to 5.1.6 keep the inversion until bash51-007 and Readline 8.2.
        [ "${output}" = "ready=1 IF2bash=1 IF2other=0 MODE2emacs=1 MODE2vi=0 TERM2xterm=1 TERM2other=0 VER2new=0 VER2old=1 alive=1" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "${output}" = "ready=1 IF2bash=0 IF2other=0 MODE2emacs=0 MODE2vi=0 TERM2xterm=0 TERM2other=0 VER2new=0 VER2old=0 alive=1" ]
      ;;
    *) false ;;
  esac
}

@test "which shells start with an emacs-style editor: C-a C-k empties the typed line" {
  probe emacs-default
  case ${FAMILY} in
    bash | zsh | zsh-sh | yash | busybox | mksh) [ "${output}" = "ready=1 D5E=1 alive=1" ] ;;
    ksh93)
      case ${VERSION} in
        *93u+m*) [ "${output}" = "ready=1 D5E=1 alive=1" ] ;; # emacs mode is on by default
        *) [ "${output}" = "ready=1 D5E=0 alive=1" ] ;;       # 93u+ 2012: no editor until asked
      esac
      ;;
    posh | dash) [ "${output}" = "ready=1 D5E=0 alive=1" ] ;;
    *) false ;;
  esac
}

@test "set -o vi turns on the POSIX vi editor: ESC 0 x x edits the line" {
  # Control: without set -o vi the same keys edit nothing in any shell.
  probe vi-default
  [ "${output}" = "ready=1 V5I=0 alive=1" ]
  probe vi-set-o
  vi=${output} # the feature checks below call run again
  case ${FAMILY} in
    bash | zsh | zsh-sh | yash | mksh | ksh93) [ "${vi}" = "ready=1 V5I=1 alive=1" ] ;;
    busybox)
      if busybox_has_vi_editing; then
        [ "${vi}" = "ready=1 V5I=1 alive=1" ]
      else
        [ "${vi}" = "ready=1 V5I=0 alive=1" ]
      fi
      ;;
    dash)
      if dash_has_libedit; then
        [ "${vi}" = "ready=1 V5I=1 alive=1" ]
      else
        [ "${vi}" = "ready=1 V5I=0 alive=1" ] # set -o vi is accepted, but nothing edits
      fi
      ;;
    posh) [ "${vi}" = "ready=1 V5I=0 alive=1" ] ;; # "set: vi: bad option"
    *) false ;;
  esac
}

@test "set editing-mode vi in the init file gives the vi editor without set -o vi" {
  probe vi-inputrc
  case ${FAMILY} in
    bash) [ "${output}" = "ready=1 V5I=1 alive=1" ] ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) [ "${output}" = "ready=1 V5I=0 alive=1" ] ;;
    *) false ;;
  esac
}

@test "at the prompt the editor owns the terminal modes; a foreground command gets them back" {
  probe termios
  canonical="icanon=1 echo=1 isig=1 icrnl=1 vmin=1 vtime=0"
  case ${FAMILY} in
    bash) at_prompt="icanon=0 echo=0 isig=1 icrnl=0 vmin=1 vtime=0" ;;
    zsh | zsh-sh) at_prompt="icanon=0 echo=0 isig=1 icrnl=1 vmin=1 vtime=0" ;;
    mksh) at_prompt="icanon=0 echo=0 isig=0 icrnl=0 vmin=1 vtime=0" ;;
    yash) at_prompt="icanon=0 echo=0 isig=0 icrnl=0 vmin=0 vtime=0" ;;
    busybox) at_prompt="icanon=0 echo=0 isig=0 icrnl=1 vmin=1 vtime=0" ;;
    ksh93)
      case ${VERSION} in
        *93u+m*) at_prompt="icanon=0 echo=0 isig=0 icrnl=0 vmin=1 vtime=0" ;;
        *) at_prompt=${canonical} ;; # 93u+ 2012 with no editing mode reads in canonical mode
      esac
      ;;
    posh | dash) at_prompt=${canonical} ;; # the kernel line discipline edits the line
    *) false ;;
  esac
  [ "${output}" = "ready=1
prompt: ${at_prompt}
command: ${canonical}
alive=1" ]
}

@test "enable-bracketed-paste decides whether a pasted newline runs a command" {
  probe paste-on
  on=${output}
  probe paste-off
  off=${output}
  pasted_as_text="ready=1 mode2004=1 before_enter=0 after_enter=2 alive=1"
  pasted_as_keys="ready=1 mode2004=0 before_enter=2 after_enter=2 alive=1"
  case ${FAMILY} in
    bash)
      if bash_at_least 4 4; then
        [ "${on}" = "${pasted_as_text}" ]
      else
        [ "${on}" = "${pasted_as_keys}" ] # Readline before 7.0 has no such variable
      fi
      [ "${off}" = "${pasted_as_keys}" ]
      ;;
    zsh | zsh-sh)
      # ZLE never reads the init file and brackets pastes on its own.
      [ "${on}" = "${pasted_as_text}" ]
      [ "${off}" = "${pasted_as_text}" ]
      ;;
    yash | busybox | posh | mksh | ksh93 | dash)
      [ "${on}" = "${pasted_as_keys}" ]
      [ "${off}" = "${pasted_as_keys}" ]
      ;;
    *) false ;;
  esac
}

@test "bracketed paste by default: on from bash 5.1 unless TERM is dumb, always on in zsh" {
  probe paste-default
  xterm=${output}
  probe paste-dumb
  dumb=${output}
  pasted_as_text="ready=1 mode2004=1 before_enter=0 after_enter=2 alive=1"
  pasted_as_keys="ready=1 mode2004=0 before_enter=2 after_enter=2 alive=1"
  case ${FAMILY} in
    bash)
      if bash_at_least 5 1; then
        [ "${xterm}" = "${pasted_as_text}" ]
      else
        [ "${xterm}" = "${pasted_as_keys}" ]
      fi
      [ "${dumb}" = "${pasted_as_keys}" ]
      ;;
    zsh | zsh-sh)
      [ "${xterm}" = "${pasted_as_text}" ]
      [ "${dumb}" = "${pasted_as_text}" ]
      ;;
    yash | busybox | posh | mksh | ksh93 | dash)
      [ "${xterm}" = "${pasted_as_keys}" ]
      [ "${dumb}" = "${pasted_as_keys}" ]
      ;;
    *) false ;;
  esac
}

@test "the builtin that rebinds keys: bind in bash and mksh, bindkey in zsh and yash, none elsewhere" {
  run_lab_c 'command -v bind; command -v bindkey; :'
  [ "${status}" -eq 0 ]
  case ${FAMILY} in
    bash | mksh) [ "${output}" = "bind" ] ;;
    zsh | yash) [ "${output}" = "bindkey" ] ;;
    zsh-sh | busybox | posh | ksh93 | dash) [ "${output}" = "" ] ;;
    *) false ;;
  esac
}

@test "bind parses init-file lines without a terminal from bash 4.0; bash 3.2 fails silently" {
  # shellcheck disable=SC2016 # the code belongs to the shell under test
  run_lab_c 'INPUTRC=/dev/null; export INPUTRC
bind "\"\\C-xq\": \"hello\"" 2>/dev/null
bind -s 2>/dev/null
echo "status=$?"'
  [ "${status}" -eq 0 ]
  case ${FAMILY} in
    bash)
      if bash_at_least 4 0; then
        [ "${output}" = '"\C-xq": "hello"
status=0' ]
      else
        [ "${output}" = "status=1" ]
      fi
      ;;
    mksh) [ "${output}" = "status=1" ] ;; # its own bind: "can't bind, not a tty"
    zsh | zsh-sh | yash | busybox | posh | ksh93 | dash) [ "${output}" = "status=127" ] ;;
    *) false ;;
  esac
}

@test "a misspelled variable is reported from bash 4.4; a misspelled function never is" {
  rc="${BATS_TEST_TMPDIR}/typos.rc"
  printf '%s\n' 'set no-such-variable on' '"\C-xz": no-such-function' >"${rc}"
  # shellcheck disable=SC2016 # the code belongs to the shell under test
  run_lab_c 'INPUTRC=/dev/null; export INPUTRC
bind -f "$1" 2>&1
echo "status=$?"' sh "${rc}"
  [ "${status}" -eq 0 ]
  # No release names the function: binding a key to an unknown function unbinds it silently.
  case ${output} in
    *no-such-function*) false ;;
    *) ;;
  esac
  case ${FAMILY} in
    bash)
      if bash_at_least 4 4; then
        case ${output} in
          *"readline: ${rc}: line 1: no-such-variable: unknown variable name"*"status=0") ;;
          *) false ;;
        esac
      elif bash_at_least 4 0; then
        # Readline 6.x: neither typo is reported; only bash's own warning is printed.
        case ${output} in
          *no-such-variable*) false ;;
          *"bind: warning: line editing not enabled"*"status=0") ;;
          *) false ;;
        esac
      else
        [ "${output}" = "status=1" ]
      fi
      ;;
    mksh)
      case ${output} in
        *"bind: can't bind, not a tty"*"status=1") ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | ksh93 | dash)
      case ${output} in
        *"status=127") ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}
