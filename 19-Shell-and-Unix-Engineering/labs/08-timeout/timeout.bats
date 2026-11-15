#!/usr/bin/env bats
# shellcheck disable=SC2016 # single-quoted code is expanded by the shell under test
# Tests for timeout(1) (POSIX.1-2024 XCU timeout): exit statuses, signal fan-out, and dispositions.
# The shell under test changes how a signal death is reported (128+N, 256+N in ksh93, 384+N in
# yash); the timeout implementation (GNU, BSD and macOS, BusyBox, uutils) changes everything else,
# so each test branches on the implementation and then on the shell family.
# TIMEOUT selects the binary (default: timeout on PATH), for example TIMEOUT=/usr/bin/timeout.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  export T="${TIMEOUT:-timeout}"
  export GONE="$LAB/gone.sh"
  cd "$BATS_TEST_TMPDIR" || return 1
  impl_line=$(TIMEOUT="$T" sh "$LAB/timeout_impl.sh")
  IMPL=${impl_line%% *}
  IMPL_VERSION=${impl_line#* }
  FAMILY=$(lab_shell_family)
}

# last: set LAST to the final line of output; a shell's "Terminated" notice on stderr can precede it.
last() {
  LAST=${lines[${#lines[@]} - 1]}
}

# sig N: set SIG to the $? the shell under test reports for a process killed by signal N.
sig() {
  case $FAMILY in
    bash | zsh | zsh-sh | dash | busybox | mksh | posh) SIG=$((128 + $1)) ;;
    ksh93) SIG=$((256 + $1)) ;;
    yash) SIG=$((384 + $1)) ;;
    *) SIG="unexpected shell family $FAMILY" ;;
  esac
}

# gnu_at_least MAJOR MINOR: succeed when the GNU timeout under test is at least MAJOR.MINOR.
gnu_at_least() {
  local major=${IMPL_VERSION%%.*} minor=${IMPL_VERSION#*.}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

@test "the implementation probe names a known timeout" {
  run sh "$LAB/timeout_impl.sh"
  [ "$status" -eq 0 ]
  case $IMPL in
    gnu)
      [[ $output =~ ^gnu\ [0-9]+\.[0-9]+$ ]]
      version_text=$("$T" --version)
      [[ ${version_text%%$'\n'*} == *" $IMPL_VERSION" ]]
      ;;
    uutils | busybox | bsd) [ "$output" = "$IMPL" ] ;;
    *) false ;;
  esac
}

@test "the command's own exit status passes through unchanged" {
  run_lab_c '"$T" 5 /bin/sh -c "exit 3"; echo "$?"'
  last
  [ "$LAST" = 3 ]
}

@test "a command still running at the limit is signalled and timeout exits 124" {
  run_lab_c '"$T" 1 /bin/sleep 5; echo "$?"'
  last
  sig 15
  case $IMPL in
    gnu | bsd | uutils) [ "$LAST" = 124 ] ;;
    # BusyBox execs the command in place, so the shell sees the command's own SIGTERM death.
    busybox) [ "$LAST" = "$SIG" ] ;;
    *) false ;;
  esac
}

@test "a fractional duration is honored, not truncated to zero" {
  run_lab_c '"$T" 0.5 /bin/sleep 3; echo "$?"'
  last
  sig 15
  case $IMPL in
    gnu | bsd | uutils) [ "$LAST" = 124 ] ;;
    busybox) [ "$LAST" = "$SIG" ] ;;
    *) false ;;
  esac
}

@test "a duration of 0 disables the limit, except in BusyBox" {
  run_lab_c '"$T" 0 /bin/sleep 1; echo "$?"'
  last
  sig 15
  case $IMPL in
    gnu | bsd | uutils) [ "$LAST" = 0 ] ;;
    # BusyBox's watcher sleeps 0 seconds and then sends the signal at once.
    busybox) [ "$LAST" = "$SIG" ] ;;
    *) false ;;
  esac
}

@test "an invalid duration is a timeout error: 125, but 1 in BusyBox" {
  run_lab_c '"$T" 1x true; echo "$?"'
  last
  case $IMPL in
    gnu | bsd | uutils) [ "$LAST" = 125 ] ;;
    busybox) [ "$LAST" = 1 ] ;;
    *) false ;;
  esac
}

@test "a missing command gives 127 and a non-executable file gives 126" {
  printf '#!/bin/sh\n' >noexec
  chmod -x noexec
  run_lab_c '"$T" 1 /nonexistent/cmd; a=$?; "$T" 1 ./noexec; echo "$a $?"'
  last
  [ "$LAST" = "127 126" ]
}

@test "timeout cannot run a shell function: it execs, so the name is not found" {
  run_lab_c 'f() { :; }; f && "$T" 1 f; echo "$?"'
  last
  [ "$LAST" = 127 ]
}

@test "a command killed by a signal makes timeout die of the same signal" {
  run_lab_c '"$T" 5 /bin/sh -c "kill -s TERM \$\$"; echo "$?"'
  last
  sig 15
  # POSIX requires timeout to re-raise the signal, so ksh93 and yash show 271 and 399, not 143.
  [ "$LAST" = "$SIG" ]
}

@test "-s KILL without -f kills a group-based timeout too; with -f the result differs" {
  run_lab_c '"$T" -s KILL 1 /bin/sleep 5; echo "$?"'
  last
  sig 9
  case $IMPL in
    gnu | uutils | busybox) [ "$LAST" = "$SIG" ] ;;
    bsd)
      case $(uname -s) in
        Darwin) [ "$LAST" = "$SIG" ] ;;
        FreeBSD) [ "$LAST" = 124 ] ;; # PROC_REAP_KILL does not signal the reaper itself
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
  run_lab_c '"$T" -f -s KILL 1 /bin/sleep 5; echo "$?"'
  last
  case $IMPL in
    gnu | uutils) [ "$LAST" = 137 ] ;; # a plain exit(137), so ksh93 shows 137 too
    bsd) [ "$LAST" = 124 ] ;;
    busybox) [ "$LAST" = 1 ] ;; # no -f option: usage error
    *) false ;;
  esac
}

@test "-p reports the signal: GNU as exit 128+N, BSD by dying of it" {
  run_lab_c '"$T" -p 1 /bin/sleep 5; echo "$?"'
  last
  sig 15
  case $IMPL in
    gnu | uutils) [ "$LAST" = 143 ] ;;
    bsd) [ "$LAST" = "$SIG" ] ;;
    busybox) [ "$LAST" = 1 ] ;; # no -p option on MMU builds
    *) false ;;
  esac
}

@test "the signal reaches descendants in the process group, but not with -f" {
  run_lab_c '"$T" 1 /bin/sh -c "/bin/sleep 8 & echo \$! >gc.pid; wait"; a=$?
    g=$(cat gc.pid); r=$(sh "$GONE" "$g" 2); [ "$r" = gone ] || kill "$g"; echo "$a $r"'
  last
  sig 15
  case $IMPL in
    gnu | bsd | uutils) [ "$LAST" = "124 gone" ] ;;
    # BusyBox signals only the PID it watches, so the grandchild survives.
    busybox) [ "$LAST" = "$SIG running" ] ;;
    *) false ;;
  esac
  rm -f gc.pid
  run_lab_c '"$T" -f 1 /bin/sh -c "/bin/sleep 8 & echo \$! >gc.pid; wait"; a=$?
    r=none; if [ -s gc.pid ]; then g=$(cat gc.pid); r=$(sh "$GONE" "$g" 2); [ "$r" = gone ] || kill "$g"; fi; echo "$a $r"'
  last
  case $IMPL in
    gnu | bsd | uutils) [ "$LAST" = "124 running" ] ;;
    busybox) [ "$LAST" = "1 none" ] ;; # -f is a usage error; nothing ran
    *) false ;;
  esac
}

@test "a descendant that calls setsid escapes a process-group timeout" {
  "${CC:-cc}" -O2 -Wall -Wextra -o setsid_exec "$LAB/setsid_exec.c"
  run_lab_c '"$T" 1 /bin/sh -c "./setsid_exec /bin/sleep 8 & echo \$! >gc.pid; wait"; a=$?
    g=$(cat gc.pid); r=$(sh "$GONE" "$g" 2); [ "$r" = gone ] || kill "$g"; echo "$a $r"'
  last
  sig 15
  case $IMPL in
    gnu | uutils) [ "$LAST" = "124 running" ] ;;
    bsd)
      case $(uname -s) in
        Darwin) [ "$LAST" = "124 running" ] ;; # process group, per the macOS page
        FreeBSD) [ "$LAST" = "124 gone" ] ;;   # procctl reaper reaches every descendant
        *) false ;;
      esac
      ;;
    busybox) [ "$LAST" = "$SIG running" ] ;;
    *) false ;;
  esac
}

@test "a stopped command is continued so the signal can act" {
  run_lab_c '"$T" -k 2 1 /bin/sh -c "echo \$\$ >stop.pid; kill -s STOP \$\$; exit 7"; a=$?
    p=$(cat stop.pid); r=$(sh "$GONE" "$p" 2); [ "$r" = gone ] || kill -s KILL "$p"; echo "$a $r"'
  last
  sig 9
  case $IMPL in
    gnu | uutils) [ "$LAST" = "124 gone" ] ;; # SIGTERM, then SIGCONT
    bsd)
      case $(uname -s) in
        # macOS waitid(WEXITED) also reports the stop, so timeout exits 1 at once; the orphaned
        # process group then gets SIGHUP and SIGCONT from the kernel, which ends the child.
        Darwin) [ "$LAST" = "1 gone" ] ;;
        FreeBSD) [ "$LAST" = "124 gone" ] ;;
        *) false ;;
      esac
      ;;
    # BusyBox sends no SIGCONT; the SIGTERM stays pending until the -k SIGKILL.
    busybox) [ "$LAST" = "$SIG gone" ] ;;
    *) false ;;
  esac
}

@test "a terminating signal sent to timeout is forwarded and the child dies with it" {
  run_lab_c '"$T" 10 /bin/sh -c "echo \$\$ >c.pid; exec /bin/sleep 8" & p=$!
    i=0; while [ ! -s c.pid ] && [ "$i" -lt 50 ]; do /bin/sleep 0.1; i=$((i + 1)); done
    kill -s TERM "$p"; wait "$p"; a=$?
    c=$(cat c.pid); r=$(sh "$GONE" "$c" 2); [ "$r" = gone ] || kill "$c"; echo "$a $r"'
  last
  sig 15
  [ "$LAST" = "$SIG gone" ]
}

@test "an inherited ignored SIGHUP reaches the command (POSIX; GNU before 9.10 reset it)" {
  run_lab_c 'trap "" HUP; "$T" 5 /bin/sh -c "kill -s HUP \$\$; exit 0"; echo "$?"'
  last
  sig 1
  case $IMPL in
    gnu)
      if gnu_at_least 9 10; then
        [ "$LAST" = 0 ]
      else
        [ "$LAST" = "$SIG" ]
      fi
      ;;
    bsd | busybox | uutils) [ "$LAST" = 0 ] ;;
    *) false ;;
  esac
}

@test "a command killed at the limit loses output still in its stdio buffer" {
  # grep has matched "line" but holds it in a block buffer (stdout is a pipe) while it waits for
  # more input; SIGTERM's default action ends it without the flush that exit() would do.
  run_lab_c 'out=$({ echo line; /bin/sleep 3; } | "$T" 1 grep line); echo "[$out]"'
  last
  [ "$LAST" = "[]" ]
}

@test "deadline.sh passes a normal exit through as exit N" {
  run_lab "$LAB/deadline.sh" 5 /bin/sh -c 'exit 3'
  last
  [ "$status" -eq 1 ]
  [ "$LAST" = "exit 3" ]
}

@test "deadline.sh reports a time-out" {
  run_lab "$LAB/deadline.sh" 1 /bin/sleep 5
  last
  [ "$status" -eq 1 ]
  case $IMPL in
    gnu | bsd | uutils) [ "$LAST" = "timed out" ] ;;
    busybox) [ "$LAST" = "signal 15" ] ;;
    *) false ;;
  esac
}

@test "deadline.sh reports a missing command" {
  run_lab "$LAB/deadline.sh" 5 /nonexistent/cmd
  last
  [ "$status" -eq 1 ]
  [ "$LAST" = "not found" ]
}

@test "deadline.sh decodes the KILL backstop in every shell's numbering" {
  run_lab "$LAB/deadline.sh" 1 /bin/sh -c 'trap "" TERM; exec /bin/sleep 5'
  last
  [ "$status" -eq 1 ]
  case $IMPL in
    gnu | uutils | busybox) [ "$LAST" = "signal 9" ] ;;
    bsd)
      case $(uname -s) in
        Darwin) [ "$LAST" = "signal 9" ] ;;
        FreeBSD) [ "$LAST" = "timed out" ] ;; # the reaper survives its own SIGKILL fan-out
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}
