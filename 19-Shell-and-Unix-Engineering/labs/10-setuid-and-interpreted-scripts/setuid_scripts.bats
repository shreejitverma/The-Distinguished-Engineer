#!/usr/bin/env bats
# What a set-ID interpreted script is exposed to, shown without privileges:
# how the kernel builds the interpreter's argv from a #! line, the "-i" trapdoor and its "-" fix,
# which shells have a privileged mode, and the environment channels (BASH_ENV, exported
# functions, IFS) that let a caller run code inside someone else's script.

setup() {
  load ../lib/lab
  T=$BATS_TEST_TMPDIR
  VICTIM="$BATS_TEST_DIRNAME/victim.sh"
  PAYLOAD="$BATS_TEST_DIRNAME/payload.sh"
  EXECPROBE="$T/execprobe"
  SHOWARGV="$T/showargv"
  # A missing compiler must fail the test, never skip it.
  "${CC:-cc}" -o "$EXECPROBE" "$BATS_TEST_DIRNAME/execprobe.c"
  "${CC:-cc}" -o "$SHOWARGV" "$BATS_TEST_DIRNAME/showargv.c"
  printf 'data\n' >"$T/data.txt"
}

# shebang_interp: print an absolute interpreter path that starts the shell under test from a #! line.
# A #! line carries at most one argument on Linux, so a multi-word LAB_SHELL becomes a symlink whose
# name selects the mode: "busybox ash" -> a link named ash (BusyBox picks the applet from argv[0]),
# "zsh --emulate sh" -> a link named sh (zsh emulates sh when invoked as sh).
shebang_interp() {
  local first
  lab_shell_words
  first=$(command -v "${LAB_SHELL_WORDS[0]}") || return 1
  case "${LAB_SHELL_WORDS[*]}" in
    "${LAB_SHELL_WORDS[0]}") printf '%s\n' "$first" ;;
    *" ash")
      ln -s "$first" "$T/ash"
      printf '%s\n' "$T/ash"
      ;;
    *" --emulate sh")
      ln -s "$first" "$T/sh"
      printf '%s\n' "$T/sh"
      ;;
    *) return 1 ;;
  esac
}

# make_dash_i_script SUFFIX: create $T/d/-i whose #! line is the shell under test plus SUFFIX,
# and $T/stdin.txt, the commands an interactive shell would read instead of the script.
make_dash_i_script() {
  local interp
  interp=$(shebang_interp)
  mkdir "$T/d"
  printf '#!%s%s\necho script-ran\n' "$interp" "$1" >"$T/d/-i"
  chmod +x "$T/d/-i"
  # shellcheck disable=SC2016 # $- belongs to the shell that reads this file
  printf 'case $- in *i*) echo mode=interactive ;; *) echo mode=other ;; esac\nexit 0\n' >"$T/stdin.txt"
}

@test "the kernel hands the #! interpreter the pathname as passed, not the link target or a descriptor" {
  printf '#!%s -x  -y\n' "$SHOWARGV" >"$T/target.sh"
  chmod +x "$T/target.sh"
  ln -s target.sh "$T/link"
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'cd "$1" && "$2" ./link one' inline "$T" "$EXECPROBE"
  [ "$status" -eq 0 ]
  case $(uname -s) in
    Linux)
      # binfmt_script.c: everything after the interpreter name is one argument, spaces included.
      [ "${#lines[@]}" -eq 4 ]
      [ "${lines[0]}" = "argv[0]=<$SHOWARGV>" ]
      [ "${lines[1]}" = "argv[1]=<-x  -y>" ]
      [ "${lines[2]}" = "argv[2]=<./link>" ]
      [ "${lines[3]}" = "argv[3]=<one>" ]
      ;;
    Darwin)
      # XNU exec_shell_imgact: the #! text is split on whitespace into separate arguments.
      [ "${#lines[@]}" -eq 5 ]
      [ "${lines[0]}" = "argv[0]=<$SHOWARGV>" ]
      [ "${lines[1]}" = "argv[1]=<-x>" ]
      [ "${lines[2]}" = "argv[2]=<-y>" ]
      [ "${lines[3]}" = "argv[3]=<./link>" ]
      [ "${lines[4]}" = "argv[4]=<one>" ]
      ;;
    *) false ;;
  esac
}

@test "a script named -i behind a bare #! line starts an interactive shell instead of running" {
  make_dash_i_script ''
  # execprobe passes "-i" to execve unchanged; the kernel then runs: INTERP -i
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'cd "$1/d" && HOME=$1 ZDOTDIR=$1 ENV= "$2" -i <"$1/stdin.txt" 2>/dev/null' inline "$T" "$EXECPROBE"
  [ "$status" -eq 0 ]
  # System rc files of an interactive shell may print escape sequences, so look for the marker line.
  [[ "$output" == *mode=interactive* ]]
  [[ "$output" != *script-ran* ]]
}

@test "a lone - after the interpreter on the #! line closes the -i trapdoor in every shell" {
  make_dash_i_script ' -'
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'cd "$1/d" && HOME=$1 ZDOTDIR=$1 ENV= "$2" -i <"$1/stdin.txt" 2>/dev/null' inline "$T" "$EXECPROBE"
  [ "$status" -eq 0 ]
  [ "$output" = script-ran ]
}

@test "set -p sticks in bash and zsh, is refused while IDs match in ksh93 and mksh, and is absent in posh, yash and BusyBox" {
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c '
    if (set -p) 2>/dev/null; then
      set -p
      case $- in *p*) short=accepted-p-on ;; *) short=accepted-p-off ;; esac
    else
      short=rejected
    fi
    if (set -o privileged) 2>/dev/null; then long=long-ok; else long=long-rejected; fi
    echo "$short $long"'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh) [ "$output" = "accepted-p-on long-ok" ] ;;
    # ksh93 and mksh refuse to turn privileged mode on when the real and effective IDs already match.
    ksh93 | mksh) [ "$output" = "accepted-p-off long-ok" ] ;;
    # Upstream dash has no -p; Debian's dash adds it (patch 9001-Add-privmode.diff), so either build passes.
    dash) [ "$output" = "accepted-p-on long-ok" ] || [ "$output" = "rejected long-rejected" ] ;;
    posh | yash | busybox) [ "$output" = "rejected long-rejected" ] ;;
    *) false ;;
  esac
}

@test "BASH_ENV runs caller-chosen code before a non-interactive script only in bash outside POSIX mode" {
  local mode
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'case :${SHELLOPTS-}: in *:posix:*) echo posix ;; *) echo native ;; esac'
  mode=$output
  lab_shell_words
  # The launcher exports both variables, then starts the shell under test on the victim script.
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'BASH_ENV=$1 ENV=$1; export BASH_ENV ENV; shift; exec "$@"' inline \
    "$PAYLOAD" "${LAB_SHELL_WORDS[@]}" "$VICTIM" "$T/data.txt"
  [ "$status" -eq 0 ]
  case "$(lab_shell_family):$mode" in
    bash:native)
      [ "${#lines[@]}" -eq 2 ]
      [ "${lines[0]}" = payload-ran ]
      [ "${lines[1]}" = victim:data ]
      ;;
    # bash invoked as sh (macOS /bin/sh) and every other shell read ENV only when interactive.
    bash:posix | dash:* | zsh:* | zsh-sh:* | ksh93:* | mksh:* | yash:* | posh:* | busybox:*)
      [ "$output" = victim:data ]
      ;;
    *) false ;;
  esac
}

@test "an exported BASH_FUNC_cat%% variable replaces the external cat inside a bash script" {
  lab_shell_words
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'exec env "BASH_FUNC_cat%%=() { echo hijacked; }" "$@"' inline \
    "${LAB_SHELL_WORDS[@]}" "$VICTIM" "$T/data.txt"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    # bash imports exported functions in every mode, including as sh; only privileged mode stops it.
    bash) [ "$output" = victim:hijacked ] ;;
    dash | zsh | zsh-sh | ksh93 | mksh | yash | posh | busybox) [ "$output" = victim:data ] ;;
    *) false ;;
  esac
}

@test "an IFS from the environment reaches the script in posh and is reset by every other shell" {
  lab_shell_words
  # shellcheck disable=SC2016 # expansions belong to the shell under test
  run_lab_c 'IFS=/; export IFS; exec "$@"' inline "${LAB_SHELL_WORDS[@]}" -c \
    'printf "a/b\n" | { read -r f1 f2; printf "%s\n" "$f1"; }'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    posh) [ "$output" = a ] ;;
    bash | dash | zsh | zsh-sh | ksh93 | mksh | yash | busybox) [ "$output" = a/b ] ;;
    *) false ;;
  esac
}
