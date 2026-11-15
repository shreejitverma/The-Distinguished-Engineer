#!/usr/bin/env bats
# shellcheck disable=SC2016 # single-quoted code is expanded by the shell under test
# Tests for env(1) (POSIX.1-2024 XCU env) and its shebang extensions.
# The env implementation (GNU, BSD and macOS, BusyBox, uutils) decides -S, -C, -a, and the
# signal options, so each test branches on env_impl.sh; whether the kernel splits a shebang
# line (macOS does, Linux does not) branches on uname -s.
# LAB_ENV selects the env under test (default /usr/bin/env), for example LAB_ENV=genv.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  cd "$BATS_TEST_TMPDIR" || return 1
  D=$(pwd -P)
  "${CC:-cc}" -o "$D/showargv" "$LAB/showargv.c"
  ENVBIN=$(command -v "${LAB_ENV:-/usr/bin/env}")
  impl_line=$(sh "$LAB/env_impl.sh" "$ENVBIN")
  IMPL=${impl_line%% *}
  IMPL_VERSION=${impl_line#* }
  KERNEL=$(uname -s)
}

# mkscript NAME FORMAT [ARG...]: write printf FORMAT to NAME and make it executable.
mkscript() {
  local name=$1
  shift
  # shellcheck disable=SC2059 # the format is the point: callers pass \r escapes
  printf "$@" >"$name"
  chmod +x "$name"
}

# gnu_at_least MAJOR MINOR: succeed when the GNU env under test is at least MAJOR.MINOR.
gnu_at_least() {
  local major=${IMPL_VERSION%%.*} minor=${IMPL_VERSION#*.}
  minor=${minor%%.*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# run_script NAME: run ./NAME x under the shell under test with $D first on PATH.
run_script() {
  run_lab_c 'PATH=$1:$PATH; export PATH; "$2" x; echo "status=$?"' sh "$D" "$D/$1"
}

@test "env without -S on a shebang line: Linux hands env one word and it fails 127; macOS splits it" {
  mkscript e1 '#!%s showargv one\n' "$ENVBIN"
  run_script e1
  case $KERNEL in
    Linux)
      [ "${lines[${#lines[@]} - 1]}" = status=127 ]
      [[ $output == *"showargv one"* ]]
      ;;
    Darwin) [ "$output" = "$(printf 'argc=4\n[0]<showargv>\n[1]<one>\n[2]<%s>\n[3]<x>\nstatus=0' "$D/e1")" ] ;;
    *) false ;;
  esac
}

@test "env -S splits a shebang argument where the implementation has -S" {
  mkscript e2 '#!%s -S showargv one two\n' "$ENVBIN"
  run_script e2
  split=$(printf 'argc=5\n[0]<showargv>\n[1]<one>\n[2]<two>\n[3]<%s>\n[4]<x>\nstatus=0' "$D/e2")
  case $KERNEL/$IMPL in
    Linux/gnu | Linux/uutils | Darwin/bsd | Darwin/gnu | Darwin/uutils) [ "$output" = "$split" ] ;;
    Linux/busybox) [ "${lines[${#lines[@]} - 1]}" = status=1 ] ;;
    *) false ;;
  esac
}

@test "a # in a shebang line: macOS's kernel ends the line there; env -S starts a comment only at a word start" {
  mkscript e3 '#!%s -S showargv a#b #c\n' "$ENVBIN"
  run_script e3
  case $KERNEL/$IMPL in
    Linux/gnu | Linux/uutils)
      [ "$output" = "$(printf 'argc=4\n[0]<showargv>\n[1]<a#b>\n[2]<%s>\n[3]<x>\nstatus=0' "$D/e3")" ]
      ;;
    Darwin/bsd | Darwin/gnu | Darwin/uutils)
      [ "$output" = "$(printf 'argc=4\n[0]<showargv>\n[1]<a>\n[2]<%s>\n[3]<x>\nstatus=0' "$D/e3")" ]
      ;;
    Linux/busybox) [ "${lines[${#lines[@]} - 1]}" = status=1 ] ;;
    *) false ;;
  esac
}

@test "env -S treats a carriage return as a separator, so a CRLF shebang through env -S still runs" {
  mkscript e4 '#!%s -S showargv\r\n' "$ENVBIN"
  run_script e4
  case $IMPL in
    gnu | uutils | bsd) [ "$output" = "$(printf 'argc=3\n[0]<showargv>\n[1]<%s>\n[2]<x>\nstatus=0' "$D/e4")" ] ;;
    busybox) [ "${lines[${#lines[@]} - 1]}" = status=1 ] ;;
    *) false ;;
  esac
}

@test "env -S expands \${VAR}, splits on \\_, and keeps quoted spaces" {
  run_lab_c 'X=val; export X; PATH=$2:$PATH; export PATH; "$1" -S "$3"; echo "status=$?"' \
    sh "$ENVBIN" "$D" 'showargv ${X} a\_b "c d"'
  case $IMPL in
    gnu | uutils | bsd)
      [ "$output" = "$(printf 'argc=5\n[0]<showargv>\n[1]<val>\n[2]<a>\n[3]<b>\n[4]<c d>\nstatus=0')" ]
      ;;
    busybox) [ "${lines[${#lines[@]} - 1]}" = status=1 ] ;;
    *) false ;;
  esac
}

@test "env -i starts from an empty environment and prints name=value lines to a pipe unquoted" {
  run_lab_c '"$1" -i "$1"; echo "status=$?"; "$1" -i A=1 "B=x y" "$1" | cat' sh "$ENVBIN"
  [ "$output" = "$(printf 'status=0\nA=1\nB=x y')" ]
}

@test "env -u removes one variable and keeps the rest" {
  run_lab_c '"$1" -i X=1 Y=2 "$1" -u X "$1"' sh "$ENVBIN"
  [ "$status" -eq 0 ]
  [ "$output" = Y=2 ]
}

@test "env finds only external utilities: a shell function of the same name is not found (127)" {
  run_lab_c 'f() { echo function; }; "$1" f; echo "status=$?"' sh "$ENVBIN"
  [[ $output != *function* ]]
  [ "${lines[${#lines[@]} - 1]}" = status=127 ]
}

@test "env exits 126 when the utility is found but not executable and 127 when it is missing" {
  : >"$D/noexec"
  chmod 644 "$D/noexec"
  run_lab_c '"$1" "$2/noexec"; echo "a=$?"; "$1" "$2/missing"; echo "b=$?"' sh "$ENVBIN" "$D"
  [ "${lines[1]}" = a=126 ]
  [ "${lines[3]}" = b=127 ]
}

@test "env -C changes directory before exec, except in BusyBox" {
  mkdir "$D/sub"
  run_lab_c '"$1" -C "$2" /bin/pwd -P; echo "status=$?"' sh "$ENVBIN" "$D/sub"
  case $IMPL in
    gnu | uutils | bsd) [ "$output" = "$(printf '%s\nstatus=0' "$D/sub")" ] ;;
    busybox) [ "${lines[${#lines[@]} - 1]}" = status=1 ] ;;
    *) false ;;
  esac
}

@test "env -a overrides argv[0] in GNU 9.5+ and uutils; BSD and BusyBox reject it" {
  run_lab_c '"$1" -a custom "$2" q; echo "status=$?"' sh "$ENVBIN" "$D/showargv"
  case $IMPL in
    gnu)
      if gnu_at_least 9 5; then
        [ "$output" = "$(printf 'argc=2\n[0]<custom>\n[1]<q>\nstatus=0')" ]
      else
        [ "${lines[${#lines[@]} - 1]}" = status=125 ]
      fi
      ;;
    uutils) [ "$output" = "$(printf 'argc=2\n[0]<custom>\n[1]<q>\nstatus=0')" ] ;;
    bsd | busybox) [ "${lines[${#lines[@]} - 1]}" = status=1 ] ;;
    *) false ;;
  esac
}

@test "env --default-signal=PIPE undoes an inherited ignored SIGPIPE in GNU and uutils" {
  run_lab_c 'trap "" PIPE; "$1" "$2" --sigpipe; "$1" --default-signal=PIPE "$2" --sigpipe; echo "status=$?"' \
    sh "$ENVBIN" "$D/showargv"
  [ "${lines[0]}" = SIGPIPE=ignored ]
  case $IMPL in
    gnu | uutils) [ "$output" = "$(printf 'SIGPIPE=ignored\nSIGPIPE=default\nstatus=0')" ] ;;
    bsd | busybox) [ "${lines[${#lines[@]} - 1]}" = status=1 ] ;;
    *) false ;;
  esac
}
