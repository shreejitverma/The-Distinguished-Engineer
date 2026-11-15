#!/usr/bin/env bats
# Tests for nice, renice, and ionice: increments, clamping, privilege, inheritance, and the
# differences between the GNU, uutils, BusyBox, BSD, and util-linux implementations.
# The shell under test runs probe.sh; the utilities are the ones its PATH finds, so utility
# behavior is asserted per implementation (probe.sh flavor) and per kernel (uname -s).

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  PRIO="$BATS_TEST_TMPDIR/prio"
  "${CC:-cc}" -O2 -Wall -pthread "$BATS_TEST_DIRNAME/prio.c" -o "$PRIO"
  KERNEL=$(uname -s)
}

# max_nice: the largest nice value the kernel accepts (sys/resource.h PRIO_MAX on the BSDs).
max_nice() {
  case $KERNEL in
    Linux) echo 19 ;;
    Darwin) echo 20 ;;
    *)
      echo "unknown kernel $KERNEL" >&2
      return 1
      ;;
  esac
}

# flavor TOOL: the implementation behind TOOL as seen by the shell under test.
flavor() {
  run_lab "$PROBE" flavor "$1"
  [ "$status" -eq 0 ]
  printf '%s\n' "$output"
}

@test "nice with no options raises the nice value by 10, clamped at the kernel maximum" {
  impl=$(flavor nice)
  case $impl in
    gnu | uutils | busybox | bsd) ;;
    *) false ;;
  esac
  run_lab "$PROBE" default "$PRIO"
  [ "$status" -eq 0 ]
  [[ $output =~ ^base=(-?[0-9]+)\ new=(-?[0-9]+)$ ]]
  base=${BASH_REMATCH[1]}
  max=$(max_nice)
  want=$((base + 10 > max ? max : base + 10))
  [ "${BASH_REMATCH[2]}" -eq "$want" ]
}

@test "nice -n 100 is clamped silently: 19 on Linux, 20 (PRIO_MAX) on macOS" {
  run_lab "$PROBE" clamp "$PRIO"
  [ "$status" -eq 0 ]
  case $KERNEL in
    Linux) [[ $output =~ \ new=19$ ]] ;;
    Darwin) [[ $output =~ \ new=20$ ]] ;;
    *) false ;;
  esac
}

@test "an unprivileged negative increment: GNU, uutils, and BSD nice warn and still run; BusyBox nice exits 1" {
  impl=$(flavor nice)
  lower=$("$PRIO" can-lower)
  run_lab "$PROBE" negative "$PRIO"
  [ "$status" -eq 0 ]
  [[ $output =~ ^base=(-?[0-9]+)\  ]]
  base=${BASH_REMATCH[1]}
  case $lower in
    lower=allowed)
      # Privileged, or RLIMIT_NICE permits it: every implementation lowers and runs.
      [ "$output" = "base=$base inner=$base status=0 warned=no" ]
      ;;
    "lower=denied errno=EACCES" | "lower=denied errno=EPERM")
      case $impl in
        gnu | uutils | bsd) [ "$output" = "base=$base inner=$((base + 1)) status=0 warned=yes" ] ;;
        busybox) [ "$output" = "base=$base inner=none status=1 warned=yes" ] ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "children inherit the nice value; native zsh (BG_NICE) runs background jobs 5 higher" {
  run_lab "$PROBE" inherit "$PRIO"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh) [ "$output" = "subshell=+0 background=+5" ] ;;
    bash | zsh-sh | dash | ksh93 | mksh | yash | busybox | posh)
      [ "$output" = "subshell=+0 background=+0" ]
      ;;
    *) false ;;
  esac
}

@test "nice can run only an executable: a shell function gives 127, a non-executable file 126" {
  impl=$(flavor nice)
  case $impl in
    gnu | uutils | busybox | bsd) ;;
    *) false ;;
  esac
  run_lab "$PROBE" exit "$PRIO"
  [ "$status" -eq 0 ]
  [ "$output" = "function=127 nonexec=126" ]
}

@test "renice -n is an increment in BSD and BusyBox, an absolute value in util-linux unless POSIXLY_CORRECT" {
  impl=$(flavor renice)
  lower=$("$PRIO" can-lower)
  run_lab "$PROBE" renice "$PRIO"
  [ "$status" -eq 0 ]
  [[ $output =~ ^base=(-?[0-9]+)\ plain=(-?[0-9]+)\ posix=(-?[0-9]+)$ ]]
  base=${BASH_REMATCH[1]}
  plain=${BASH_REMATCH[2]}
  posix=${BASH_REMATCH[3]}
  max=$(max_nice)
  [ "$((base + 4))" -le "$max" ]
  case $impl in
    bsd | busybox)
      [ "$plain" -eq $((base + 2)) ]
      [ "$posix" -eq $((base + 4)) ]
      ;;
    util-linux)
      # renice -n 1 sets the nice value to 1; lowering it needs privilege or RLIMIT_NICE.
      case $lower in
        lower=allowed) want=1 ;;
        lower=denied*) want=$((base < 1 ? 1 : base)) ;;
        *) false ;;
      esac
      [ "$plain" -eq "$want" ]
      [ "$posix" -eq $((plain + 2)) ]
      ;;
    *) false ;;
  esac
}

@test "ionice: util-linux and BusyBox fill in different class data; macOS has no ionice" {
  case $KERNEL in
    Darwin)
      run_lab_c 'command -v ionice || echo absent'
      [ "$status" -eq 0 ]
      [ "$output" = absent ]
      ;;
    Linux)
      run_lab "$PROBE" ionice "$PRIO"
      [ "$status" -eq 0 ]
      [ -n "$output" ]
      while IFS= read -r line; do
        case $line in
          util-linux\ *)
            [ "$line" = "util-linux none=[class=0 level=0] c3=[class=3 level=7] c2=[class=2 level=4] n7=[class=2 level=7] child=[class=2 level=6]" ]
            ;;
          busybox\ *)
            [ "$line" = "busybox none=[class=0 level=0] c3=[class=3 level=0] c2=[class=2 level=0] n7=[fail] child=[class=2 level=6]" ]
            ;;
          *) false ;;
        esac
      done <<<"$output"
      ;;
    *) false ;;
  esac
}

@test "setpriority on a PID changes one thread on Linux but the whole process on macOS" {
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c '"$1" threads' sh "$PRIO"
  [ "$status" -eq 0 ]
  case $KERNEL in
    Linux) [ "$output" = "main=+1 old_thread=+0 new_thread=+1" ] ;;
    Darwin) [ "$output" = "main=+1 old_thread=+1 new_thread=+1" ] ;;
    *) false ;;
  esac
}

@test "ulimit -e reports RLIMIT_NICE where both the shell and the kernel have it" {
  run_lab "$PROBE" ulimit-e "$PRIO"
  [ "$status" -eq 0 ]
  family=$(lab_shell_family)
  case $KERNEL in
    Linux)
      [[ $output =~ rlimit=([0-9]+|unlimited)$ ]]
      rlimit=${BASH_REMATCH[1]}
      case $family in
        bash | zsh | zsh-sh | ksh93 | mksh | yash | busybox)
          [ "$output" = "out=$rlimit status=0 rlimit=$rlimit" ]
          ;;
        dash) [ "$output" = "out=message status=2 rlimit=$rlimit" ] ;;
        posh) [ "$output" = "out=message status=127 rlimit=$rlimit" ] ;;
        *) false ;;
      esac
      ;;
    Darwin)
      # No RLIMIT_NICE in the macOS headers, so no shell offers it.
      case $family in
        bash | dash) [ "$output" = "out=message status=2 rlimit=none" ] ;;
        zsh | zsh-sh) [ "$output" = "out=message status=1 rlimit=none" ] ;;
        ksh93) [ "$output" = "out=message status=0 rlimit=none" ] ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}
