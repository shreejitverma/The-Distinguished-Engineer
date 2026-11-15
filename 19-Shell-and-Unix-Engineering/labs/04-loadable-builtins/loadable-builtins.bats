#!/usr/bin/env bats
# Loadable builtins: bash `enable -f`, ksh93 `builtin -f`, zsh `zmodload`, and the shells
# that have no runtime loading at all. probe.c is one shared object that serves bash and ksh93.
#
# Tests that only bash can run start with a guard that prints "not bash" and exits 3 in
# every other shell; the first three tests assert each family's own loading mechanism.

setup() {
  load ../lib/lab
  LAB_DIR="$BATS_TEST_DIRNAME"
  OBJ="$BATS_TEST_TMPDIR/obj"
  mkdir "$OBJ"
  "$LAB_DIR/build-probe.sh" "$OBJ"
  SO="$OBJ/probe.so"
}

# bash_at_least MAJOR MINOR: succeed when the bash under test is at least MAJOR.MINOR.
bash_at_least() {
  local v major minor
  v=$(lab_shell_version)
  major=${v%%.*}
  minor=${v#*.}
  minor=${minor%%.*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# assert_not_bash: the guard's result in every family that is not bash.
assert_not_bash() {
  [ "$status" -eq 3 ]
  [ "$output" = "not bash" ]
}

# shellcheck disable=SC2016 # the expansion belongs to the shell under test
NOT_BASH='[ -n "${BASH_VERSION-}" ] || { echo "not bash"; exit 3; }'

@test "enable -f installs a builtin that runs in the shell's own process" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '{ enable -f "$1" probe; } 2>/dev/null || { echo "no enable -f"; exit 3; }
    type -t probe
    probe pid >"$2/pid"
    read -r p <"$2/pid"
    [ "$p" = "$$" ] && echo "same pid as the shell"
    s=$(probe pid)
    [ "$s" != "$$" ] && echo "command substitution forked"' sh "$SO" "$BATS_TEST_TMPDIR"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = builtin ]
      [ "${lines[1]}" = "same pid as the shell" ]
      [ "${lines[2]}" = "command substitution forked" ]
      ;;
    # zsh has enable, but its -f selects the function table; the others have no enable.
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -eq 3 ]
      [ "$output" = "no enable -f" ]
      ;;
    *) false ;;
  esac
}

@test "ksh93 builtin -f loads the same object through plugin_version and b_probe" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  # The braces matter: zsh reports "no such builtin: -f" before a redirection on the
  # builtin command itself takes effect.
  run_lab_c '{ builtin -f "$1" probe; } 2>/dev/null || { echo "no builtin -f"; exit 3; }
    probe args a "b c"
    probe pid >"$2/pid"
    read -r p <"$2/pid"
    [ "$p" = "$$" ] && echo "same pid as the shell"' sh "$SO" "$BATS_TEST_TMPDIR"
  case $(lab_shell_family) in
    ksh93)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "2 args: <a> <b c>" ]
      [ "${lines[1]}" = "same pid as the shell" ]
      ;;
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | dash)
      [ "$status" -eq 3 ]
      [ "$output" = "no builtin -f" ]
      ;;
    *) false ;;
  esac
}

@test "zsh loads builtins as modules: zmodload zsh/files turns mkdir into a builtin" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '{ zmodload zsh/files; } 2>/dev/null || { echo "no zmodload"; exit 3; }
    whence -w mkdir'
  case $(lab_shell_family) in
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "mkdir: builtin" ]
      ;;
    bash | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -eq 3 ]
      [ "$output" = "no zmodload" ]
      ;;
    *) false ;;
  esac
}

@test "a loaded builtin outranks PATH, even an empty PATH; enable -n uncovers the PATH command" {
  mkdir "$BATS_TEST_TMPDIR/bin"
  printf '#!/bin/sh\necho external\n' >"$BATS_TEST_TMPDIR/bin/probe"
  chmod +x "$BATS_TEST_TMPDIR/bin/probe"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    PATH=$2
    probe args 1
    enable -f "$1" probe
    probe args 2
    enable -n probe
    probe args 3
    enable probe
    PATH=/nonexistent
    probe args 4' sh "$SO" "$BATS_TEST_TMPDIR/bin"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = external ]
      [ "${lines[1]}" = "1 args: <2>" ]
      [ "${lines[2]}" = external ]
      [ "${lines[3]}" = "1 args: <4>" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "a function outranks a loaded builtin, and the builtin command skips the function" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    enable -f "$1" probe
    probe() { echo function; }
    probe args 1
    builtin probe args 2' sh "$SO"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = function ]
      [ "${lines[1]}" = "1 args: <2>" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "a loaded builtin changes the shell's own state: chdir(2) without a PWD update" {
  mkdir -p "$BATS_TEST_TMPDIR/work/sub"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    enable -f "$1" probe
    cd "$2"
    probe cd sub
    real=$(/bin/pwd -P)
    echo "kernel cwd: ${real##*/}"
    echo "PWD: ${PWD##*/}"' sh "$SO" "$BATS_TEST_TMPDIR/work"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "kernel cwd: sub" ]
      [ "${lines[1]}" = "PWD: work" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "a fault in a loaded builtin kills the shell; a subshell contains it" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    exec 2>/dev/null
    enable -f "$1" probe
    ( probe crash )
    echo "subshell status $?"
    probe crash
    echo "not reached"' sh "$SO"
  case $(lab_shell_family) in
    bash)
      # SIGABRT is 6 on Linux and macOS; the shell dies with 128 + 6.
      [ "$status" -eq 134 ]
      [ "$output" = "subshell status 134" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "subshells inherit a loaded builtin; a new bash process does not" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    enable -f "$1" probe
    ( type -t probe )
    "$BASH" -c "type -t probe || echo absent in child bash"' sh "$SO"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = builtin ]
      [ "${lines[1]}" = "absent in child bash" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "a constructor runs inside dlopen before bash looks for the struct" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  PROBE_CTOR=1 run_lab_c "$NOT_BASH"'
    enable -f "$1" nosuch 2>/dev/null
    echo "enable status $?"' sh "$SO"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      case ${lines[0]} in
        "constructor ran in pid "[0-9]*) true ;;
        *) false ;;
      esac
      [ "${lines[1]}" = "enable status 1" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "the load hook can veto a builtin (bash 4.4+); older bash ignores the hook" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    enable -f "$1" probefail 2>/dev/null
    echo "enable status $?"
    type -t probefail || echo absent
    probefail 2>/dev/null || echo "not runnable"' sh "$SO"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      if bash_at_least 4 4; then
        [ "${lines[0]}" = "enable status 1" ]
        [ "${lines[1]}" = absent ]
        [ "${lines[2]}" = "not runnable" ]
      else
        [ "${lines[0]}" = "enable status 0" ]
        [ "${lines[1]}" = builtin ]
        [ "${lines[2]}" = "probefail ran" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "enable -d removes only loaded builtins and runs the unload hook (bash 4.4+)" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    enable -f "$1" probe
    enable -d probe
    echo "delete status $?"
    type -t probe || echo gone
    enable -d echo 2>/dev/null
    echo "delete echo status $?"' sh "$SO"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      if bash_at_least 4 4; then
        [ "$output" = "unload hook: probe
delete status 0
gone
delete echo status 1" ]
      else
        [ "$output" = "delete status 0
gone
delete echo status 1" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "a bare file name: BASH_LOADABLES_PATH (4.4+), and the current directory when it is unset in 5.3" {
  mkdir "$BATS_TEST_TMPDIR/empty"
  # Each case runs in its own subshell; 0 means the builtin loaded.
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    exec 2>/dev/null
    (cd "$2" && BASH_LOADABLES_PATH=$1 && enable -f probe.so probe)
    echo "found via BASH_LOADABLES_PATH: $?"
    (cd "$1" && BASH_LOADABLES_PATH=$2 && enable -f probe.so probe)
    echo "in cwd, missed by BASH_LOADABLES_PATH: $?"
    (cd "$1" && unset BASH_LOADABLES_PATH && enable -f probe.so probe)
    echo "in cwd, BASH_LOADABLES_PATH unset: $?"' sh "$OBJ" "$BATS_TEST_TMPDIR/empty"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      # Before 5.3 bash passes the bare name to dlopen(3), and neither glibc, musl, nor
      # the hardened macOS /bin/bash searches the current directory for a name without a slash.
      if bash_at_least 5 3; then
        [ "${lines[0]}" = "found via BASH_LOADABLES_PATH: 0" ]
        [ "${lines[1]}" = "in cwd, missed by BASH_LOADABLES_PATH: 1" ]
        [ "${lines[2]}" = "in cwd, BASH_LOADABLES_PATH unset: 0" ]
      elif bash_at_least 4 4; then
        [ "${lines[0]}" = "found via BASH_LOADABLES_PATH: 0" ]
        [ "${lines[1]}" = "in cwd, missed by BASH_LOADABLES_PATH: 1" ]
        [ "${lines[2]}" = "in cwd, BASH_LOADABLES_PATH unset: 1" ]
      else
        [ "${lines[0]}" = "found via BASH_LOADABLES_PATH: 1" ]
        [ "${lines[1]}" = "in cwd, missed by BASH_LOADABLES_PATH: 1" ]
        [ "${lines[2]}" = "in cwd, BASH_LOADABLES_PATH unset: 1" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "the default BASH_LOADABLES_PATH of bash 5.2+ ends in '.', so a bare name loads from the current directory" {
  # A fresh bash process, with the variable removed from its environment, shows the compiled-in
  # default: config-top.h from 5.2 on, nothing before. Distributions may change the list but not
  # drop the trailing '.' in the upstream build this assertion follows.
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    exec 2>/dev/null
    d=$(env -u BASH_LOADABLES_PATH "$BASH" -c "echo \"\${BASH_LOADABLES_PATH-unset}\"")
    echo "default path ends in: ${d##*:}"
    cd "$1"
    env -u BASH_LOADABLES_PATH "$BASH" -c "enable -f probe.so probe"
    echo "bare name in cwd, default path: $?"' sh "$OBJ"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      if bash_at_least 5 2; then
        [ "${lines[0]}" = "default path ends in: ." ]
        [ "${lines[1]}" = "bare name in cwd, default path: 0" ]
      else
        [ "${lines[0]}" = "default path ends in: unset" ]
        [ "${lines[1]}" = "bare name in cwd, default path: 1" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "enable NAME with no -f loads NAME from BASH_LOADABLES_PATH (bash 5.2+)" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    BASH_LOADABLES_PATH=$1
    enable probe 2>/dev/null
    echo "enable status $?"
    type -t probe || echo absent' sh "$OBJ"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      if bash_at_least 5 2; then
        [ "${lines[0]}" = "enable status 0" ]
        [ "${lines[1]}" = builtin ]
      else
        [ "${lines[0]}" = "enable status 1" ]
        [ "${lines[1]}" = absent ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "restricted mode refuses enable -f; only bash 5.3 also refuses the enable NAME form" {
  # stderr is closed before set -r, because a restricted shell refuses later redirections
  # (and a refused redirection, not enable, would then produce the status 1 we test for).
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    BASH_LOADABLES_PATH=$2
    exec 2>/dev/null
    set -r
    enable -f "$1" probe
    echo "enable -f status $?"
    enable probe
    echo "enable NAME status $?"
    type -t probe || echo absent' sh "$SO" "$OBJ"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "enable -f status 1" ]
      if bash_at_least 5 3; then
        [ "${lines[1]}" = "enable NAME status 1" ]
        [ "${lines[2]}" = absent ]
      elif bash_at_least 5 2; then
        # bash 5.2 skipped the restricted check on this path (CHANGES, bash-5.3-beta item j).
        [ "${lines[1]}" = "enable NAME status 0" ]
        [ "${lines[2]}" = builtin ]
      else
        [ "${lines[1]}" = "enable NAME status 1" ]
        [ "${lines[2]}" = absent ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "status 262 (EX_DISKFALLBACK) hands the command to PATH in bash 5.0+; older bash reports 0" {
  mkdir "$BATS_TEST_TMPDIR/bin"
  printf '#!/bin/sh\necho external\n' >"$BATS_TEST_TMPDIR/bin/probe"
  chmod +x "$BATS_TEST_TMPDIR/bin/probe"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    PATH=$2
    enable -f "$1" probe
    probe fallback
    echo "status $?"' sh "$SO" "$BATS_TEST_TMPDIR/bin"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      if bash_at_least 5 0; then
        [ "$output" = "external
status 0" ]
      else
        [ "$output" = "status 0" ]
      fi
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "enable -s -f makes a special builtin: POSIX mode keeps its prefix assignments" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    enable -s -f "$1" probe
    enable -s | grep -x "enable probe"
    set -o posix
    x=kept probe pid >/dev/null
    echo "posix mode: ${x-unset}"
    set +o posix
    y=kept probe pid >/dev/null
    echo "bash mode: ${y-unset}"' sh "$SO"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "enable probe" ]
      [ "${lines[1]}" = "posix mode: kept" ]
      [ "${lines[2]}" = "bash mode: unset" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}

@test "help prints the struct's short_doc and long_doc" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c "$NOT_BASH"'
    enable -f "$1" probe
    help probe' sh "$SO"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "probe: probe mode [arg ...]" ]
      [ "${lines[1]}" = "    Report where a loadable builtin runs." ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) assert_not_bash ;;
    *) false ;;
  esac
}
