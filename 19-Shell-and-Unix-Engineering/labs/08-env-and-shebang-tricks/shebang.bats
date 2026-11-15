#!/usr/bin/env bats
# shellcheck disable=SC2016 # single-quoted code is expanded by the shell under test
# Tests for the "#!" mechanism: what the kernel (Linux fs/binfmt_script.c, XNU
# exec_shell_imgact) builds from the first line, and what the shell under test does
# when execve fails.  The kernel decides the argument vector, so those tests branch on
# uname -s; the shell decides the fallback after ENOENT or ENOEXEC, so those branch on
# lab_shell_family.  showargv is the interpreter: it prints argv byte for byte.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  cd "$BATS_TEST_TMPDIR" || return 1
  D=$(pwd -P)
  "${CC:-cc}" -o "$D/showargv" "$LAB/showargv.c"
  "${CC:-cc}" -o "$D/execpath" "$LAB/execpath.c"
  KERNEL=$(uname -s)
  FAMILY=$(lab_shell_family)
}

# mkscript NAME FORMAT [ARG...]: write printf FORMAT to NAME and make it executable.
mkscript() {
  local name=$1
  shift
  # shellcheck disable=SC2059 # the format is the point: callers pass \r and \t escapes
  printf "$@" >"$name"
  chmod +x "$name"
}

# shell_bin: set BIN to an absolute path that runs the shell under test when used
# in a "#!" line (busybox needs a symlink named ash; zsh --emulate sh runs native zsh).
shell_bin() {
  lab_shell_words
  case $FAMILY in
    busybox)
      bb=$(command -v busybox) || return 1
      ln -s "$bb" "$D/ash"
      BIN=$D/ash
      ;;
    *) BIN=$(command -v "${LAB_SHELL_WORDS[0]}") ;;
  esac
  [ -x "$BIN" ]
}

@test "the kernel passes a multi-word shebang argument as one word on Linux and splits it on macOS" {
  mkscript s '#!%s -a  -b\n' "$D/showargv"
  run_lab_c '"$1" x' sh "$D/s"
  [ "$status" -eq 0 ]
  case $KERNEL in
    Linux) expected=$(printf 'argc=4\n[0]<%s>\n[1]<-a  -b>\n[2]<%s>\n[3]<x>' "$D/showargv" "$D/s") ;;
    Darwin) expected=$(printf 'argc=5\n[0]<%s>\n[1]<-a>\n[2]<-b>\n[3]<%s>\n[4]<x>' "$D/showargv" "$D/s") ;;
    *) expected="unexpected kernel $KERNEL" ;;
  esac
  [ "$output" = "$expected" ]
}

@test "the caller's argv[0] is discarded and the script path is passed exactly as the caller wrote it" {
  mkscript s '#!%s\n' "$D/showargv"
  run_lab_c '"$1" ./s custom0 x' sh "$D/execpath"
  [ "$status" -eq 0 ]
  case $KERNEL in
    Linux | Darwin) [ "$output" = "$(printf 'argc=3\n[0]<%s>\n[1]<./s>\n[2]<x>' "$D/showargv")" ] ;;
    *) false ;;
  esac
}

@test "trailing blanks are trimmed from the shebang line but a carriage return stays in the argument" {
  mkscript cr '#!  %s -x\r\n' "$D/showargv"
  mkscript blank '#!%s \t \n' "$D/showargv"
  run_lab_c '"$1"; "$2"' sh "$D/cr" "$D/blank"
  [ "$status" -eq 0 ]
  case $KERNEL in
    Linux | Darwin)
      expected=$(printf 'argc=3\n[0]<%s>\n[1]<-x\\015>\n[2]<%s>\nargc=2\n[0]<%s>\n[1]<%s>' \
        "$D/showargv" "$D/cr" "$D/showargv" "$D/blank")
      [ "$output" = "$expected" ]
      ;;
    *) false ;;
  esac
}

@test "a CRLF shebang names a missing interpreter: bash 126 or 127 by build, mksh 126, posh 1, the others 127" {
  mkscript crlf '#!/bin/sh\r\necho ran\r\n'
  run_lab_c '"$1"; echo "status=$?"' sh "$D/crlf"
  last=${lines[${#lines[@]} - 1]}
  [[ $output != *ran* ]]
  case $FAMILY in
    bash)
      # bash 3.2 and 5.3 on macOS: "bad interpreter", 126; bash 5.2.37 on
      # Debian 13 and Alpine 3.22: "required file not found", 127 (measured).
      if [[ $output == *"bad interpreter"* ]]; then
        [ "$last" = status=126 ]
      else
        [[ $output == *"required file not found"* ]]
        [ "$last" = status=127 ]
      fi
      ;;
    zsh | zsh-sh)
      # zsh 5.9 keeps execve's ENOENT status; zsh master exits 126 (Src/exec.c, zexecve).
      case $(lab_shell_version) in
        5.9*) [ "$last" = status=127 ] ;;
        *) [ "$last" = status=126 ] ;;
      esac
      [[ $output == *"bad interpreter"* ]]
      ;;
    mksh) [ "$last" = status=126 ] ;; # mksh R59 2020 and 2025, measured
    posh) [ "$last" = status=1 ] ;;   # posh 0.14.1, measured
    dash | ksh93 | yash | busybox) [ "$last" = status=127 ] ;;
    *) false ;;
  esac
}

@test "a script whose interpreter is a script runs on Linux; macOS refuses it with ENOEXEC unless env sits between" {
  mkscript mid '#!%s mid-arg\n' "$D/showargv"
  mkscript outer '#!%s outer-arg\n' "$D/mid"
  mkscript viaenv '#!/usr/bin/env %s\n' "$D/mid"
  run_lab_c '"$1" ./outer outer x; echo "status=$?"; "$1" ./viaenv viaenv x' sh "$D/execpath"
  via=$(printf 'argc=5\n[0]<%s>\n[1]<mid-arg>\n[2]<%s>\n[3]<./viaenv>\n[4]<x>' "$D/showargv" "$D/mid")
  case $KERNEL in
    Linux)
      nested=$(printf 'argc=6\n[0]<%s>\n[1]<mid-arg>\n[2]<%s>\n[3]<outer-arg>\n[4]<./outer>\n[5]<x>\nstatus=0' "$D/showargv" "$D/mid")
      [ "$output" = "$nested"$'\n'"$via" ]
      ;;
    Darwin) [ "$output" = "execve: ENOEXEC"$'\n'"status=126"$'\n'"$via" ] ;;
    *) false ;;
  esac
}

@test "Linux follows a script plus four interpreter scripts and fails the sixth level with ELOOP; macOS stops at one" {
  # l1 runs showargv; lN runs l(N-1), so lN is N levels of "#!" rewriting.
  mkscript l1 '#!%s\n' "$D/showargv"
  for n in 2 3 4 5 6; do
    mkscript "l$n" '#!%s\n' "$D/l$((n - 1))"
  done
  run_lab_c 'for n in 1 2 5 6; do
    out=$("$1" "./l$n" "l$n"); st=$?
    case $out in execve:*) ;; *) out=ran ;; esac
    echo "l$n=$st $out"
  done' sh "$D/execpath"
  case $KERNEL in
    Linux) [ "$output" = "$(printf 'l1=0 ran\nl2=0 ran\nl5=0 ran\nl6=126 execve: ELOOP')" ] ;;
    Darwin) [ "$output" = "$(printf 'l1=0 ran\nl2=126 execve: ENOEXEC\nl5=126 execve: ENOEXEC\nl6=126 execve: ENOEXEC')" ] ;;
    *) false ;;
  esac
}

@test "after the kernel's ENOEXEC for a nested interpreter, zsh emulates #! itself and the others run the file as sh" {
  mkscript mid '#!%s mid-arg\n' "$D/showargv"
  mkscript outer '#!%s outer-arg\n' "$D/mid"
  run_lab_c 'out=$("$1" 2>&1); echo "${out:-empty} status=$?"' sh "$D/outer"
  [ "$status" -eq 0 ]
  chain=$(printf 'argc=5\n[0]<%s>\n[1]<mid-arg>\n[2]<%s>\n[3]<outer-arg>\n[4]<%s> status=0' "$D/showargv" "$D/mid" "$D/outer")
  case $KERNEL in
    Linux) [ "$output" = "$chain" ] ;;
    Darwin)
      case $FAMILY in
        zsh | zsh-sh) [ "$output" = "$chain" ] ;;
        bash | dash | ksh93) [ "$output" = "empty status=0" ] ;;
        mksh | posh | yash | busybox) skip "not measured: the macOS nested-interpreter fallback of this shell" ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "an interpreter path longer than the kernel's buffer gives ENOEXEC; shells run the file, zsh says bad interpreter" {
  long=$D
  while [ "${#long}" -lt 600 ]; do
    long=$long/dddddddddddddddddddddddddddddddddddddddddddddddd
  done
  mkdir -p "$long"
  cp "$D/showargv" "$long/showargv"
  mkscript long '#!%s\necho ran-as-shell-script\n' "$long/showargv"
  run_lab_c '"$1" ./long long; echo "status=$?"' sh "$D/execpath"
  [ "$output" = "$(printf 'execve: ENOEXEC\nstatus=126')" ]
  run_lab_c 'out=$("$1" 2>&1); st=$?; echo "$out"; echo "status=$st"' sh "$D/long"
  last=${lines[${#lines[@]} - 1]}
  case $KERNEL in
    Linux | Darwin) ;;
    *) false ;;
  esac
  case $FAMILY in
    zsh | zsh-sh)
      [ "$last" = status=126 ]
      [[ $output == *"bad interpreter"* ]]
      ;;
    mksh)
      # Debian's mksh R59 2025 checks the shebang itself; Alpine's R59 2020 runs the file.
      if [[ $output == *"shebang too long"* ]]; then
        [ "$last" = status=126 ]
      else
        [ "$output" = "ran-as-shell-script"$'\n'"status=0" ]
      fi
      ;;
    ksh93)
      # ksh93u+m 1.0.10 aborts ("realloc(): invalid pointer", 134); ksh93u+ 2012 runs the file.
      case $(lab_shell_version) in
        *93u+m*) [ "$last" = status=134 ] ;;
        *) [ "$output" = "ran-as-shell-script"$'\n'"status=0" ] ;;
      esac
      ;;
    bash | dash | posh | yash | busybox)
      [ "$output" = "ran-as-shell-script"$'\n'"status=0" ]
      ;;
    *) false ;;
  esac
}

@test "Linux cuts a long shebang argument at its 256-byte buffer; macOS passes up to 512 bytes whole" {
  arg=$(printf '%0300d' 0)
  mkscript s '#!%s %s\n' "$D/showargv" "$arg"
  run_lab_c '"$1"' sh "$D/s"
  [ "$status" -eq 0 ]
  got=${lines[2]#\[1\]<}
  got=${got%>}
  case $KERNEL in
    # bytes 0..254 of the buffer survive: "#!", the path, one space, then the argument.
    Linux) [ "${#got}" -eq $((252 - ${#D} - 9)) ] ;;
    Darwin) [ "$got" = "$arg" ] ;;
    *) false ;;
  esac
}

@test "a relative interpreter resolves against the caller's directory; zsh alone then searches PATH" {
  mkscript rel '#!showargv rel\n'
  mkdir sub
  run_lab_c 'cd "$1" && "$2" ../rel rel; echo "status=$?"' sh "$D/sub" "$D/execpath"
  [ "$output" = "$(printf 'execve: ENOENT\nstatus=127')" ]
  run_lab_c '"$1" ./rel rel' sh "$D/execpath"
  [ "$output" = "$(printf 'argc=3\n[0]<showargv>\n[1]<rel>\n[2]<./rel>')" ]
  run_lab_c 'cd "$1" && PATH=$2:$PATH && export PATH && ../rel; echo "status=$?"' sh "$D/sub" "$D"
  last=${lines[${#lines[@]} - 1]}
  case $FAMILY in
    zsh | zsh-sh) [ "$output" = "$(printf 'argc=3\n[0]<showargv>\n[1]<rel>\n[2]<../rel>\nstatus=0')" ] ;;
    bash)
      if [[ $output == *"bad interpreter"* ]]; then [ "$last" = status=126 ]; else [ "$last" = status=127 ]; fi
      ;;
    mksh) [ "$last" = status=126 ] ;;
    posh) [ "$last" = status=1 ] ;;
    dash | ksh93 | yash | busybox) [ "$last" = status=127 ] ;;
    *) false ;;
  esac
}

@test "a script named like an option is read as one unless the shebang ends with a lone -" {
  shell_bin
  mkdir plain dashed
  mkscript plain/-x '#!%s\necho ran-as-file\n' "$BIN"
  mkscript dashed/-x '#!%s -\necho ran-as-file\n' "$BIN"
  # execve("-x"): the kernel hands the interpreter the bare name "-x" as its script operand.
  run_lab_c 'cd "$1" && printf "echo from-stdin\n" | "$2" -x -x 2>/dev/null' sh "$D/plain" "$D/execpath"
  [ "$output" = from-stdin ]
  run_lab_c 'cd "$1" && printf "echo from-stdin\n" | "$2" -x -x 2>/dev/null' sh "$D/dashed" "$D/execpath"
  case $FAMILY in
    bash | zsh | zsh-sh | dash | ksh93 | mksh | posh | yash | busybox) [ "$output" = ran-as-file ] ;;
    *) false ;;
  esac
}

@test "a re-exec trampoline hands its own path and every argument to the new interpreter" {
  REEXEC_WANT="$D/showargv" run_lab "$LAB/reexec.sh" a 'b c' ''
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'argc=5\n[0]<%s>\n[1]<%s>\n[2]<a>\n[3]<b c>\n[4]<>' "$D/showargv" "$LAB/reexec.sh")" ]
  REEXEC_WANT="$D/showargv" run_lab "$LAB/reexec.sh"
  [ "$output" = "$(printf 'argc=2\n[0]<%s>\n[1]<%s>' "$D/showargv" "$LAB/reexec.sh")" ]
}

@test "the trampoline's guard variable makes re-executing into the same shell happen exactly once" {
  shell_bin
  REEXEC_WANT="$BIN" run_lab "$LAB/reexec.sh" a 'b c'
  [ "$status" -eq 0 ]
  [ "$output" = "native reexec=1: <a> <b c>" ]
  REEXEC_WANT=no-such-interpreter-here run_lab "$LAB/reexec.sh" a
  [ "$output" = "native reexec=unset: <a>" ]
}
