#!/usr/bin/env bats
# Tests for "xargs -P": bounded parallelism, completion-order output, failure handling, GNU signals.
# Every probe runs through run_lab, so the shell under test is the one that drives xargs; the
# workers themselves run under /bin/sh because xargs exec()s them through their shebang line.
# The behavior that differs is the xargs implementation, so those tests branch on xargs_flavor.sh.

# shellcheck disable=SC2016 # code strings run in the shell under test, so $1 and $2 must not expand here

setup() {
  load ../lib/lab
  HERE="$BATS_TEST_DIRNAME"
  DIR="$BATS_TEST_TMPDIR"
  FLAVOR=$("$HERE/xargs_flavor.sh")
}

@test "the xargs under test is a flavor this lab knows" {
  case $FLAVOR in
    gnu | busybox | bsd) ;;
    *) false ;;
  esac
}

@test "without -P jobs run one at a time (POSIX: wait for each invocation)" {
  run_lab "$HERE/run_pool.sh" default 3 1 "$DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "peak=1 invocations=3 status=0" ]
}

@test "-P 1 is serial" {
  run_lab "$HERE/run_pool.sh" 1 3 1 "$DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "peak=1 invocations=3 status=0" ]
}

@test "-P 3 runs exactly three jobs at once and finishes in about a third of the serial time" {
  start=$(date +%s)
  run_lab "$HERE/run_pool.sh" 3 9 1 "$DIR"
  elapsed=$(($(date +%s) - start))
  [ "$status" -eq 0 ]
  [ "$output" = "peak=3 invocations=9 status=0" ]
  # 9 one-second jobs in waves of 3 take about 3 s; serial would take at least 9 s.
  [ "$elapsed" -lt 8 ]
}

@test "-P 0 lifts the cap: six jobs run at once (GNU unlimited, BSD rlimit, BusyBox 100)" {
  run_lab "$HERE/run_pool.sh" 0 6 1 "$DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "peak=6 invocations=6 status=0" ]
}

@test "-P does nothing without -n: all short input goes into one invocation" {
  rm -f "$DIR/peak.log"
  run_lab_c 'seq 1 8 | "$1" -P 4 "$2" "$3" 1' _ "${XARGS:-xargs}" "$HERE/peak_worker.sh" "$DIR"
  [ "$status" -eq 0 ]
  nlines=$(wc -l <"$DIR/peak.log")
  sort -n -o "$DIR/peak.sorted" "$DIR/peak.log"
  peak=$(tail -n 1 "$DIR/peak.sorted")
  [ "$((nlines))" -eq 1 ]
  [ "$peak" -eq 1 ]
}

@test "output order under -P is completion order, not input order" {
  run_lab_c 'printf "2\n1\n" | "$1" -n 1 "$2"' _ "${XARGS:-xargs}" "$HERE/sleep_echo.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf '2\n1')" ]
  run_lab_c 'printf "2\n1\n" | "$1" -P 2 -n 1 "$2"' _ "${XARGS:-xargs}" "$HERE/sleep_echo.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf '1\n2')" ]
}

@test "xargs does not return until every in-flight job has finished" {
  run_lab "$HERE/exit_demo.sh" 2 "$DIR" slow
  [ "$status" -eq 0 ]
  [ "$output" = "status=0 seen=slow, slow_done=yes" ]
}

@test "a failing job does not stop the others: exit 123 (GNU, BusyBox) or 1 (BSD)" {
  run_lab "$HERE/exit_demo.sh" 1 "$DIR" bad ok ok
  case $FLAVOR in
    gnu | busybox) [ "$output" = "status=123 seen=bad,ok,ok, slow_done=no" ] ;;
    bsd) [ "$output" = "status=1 seen=bad,ok,ok, slow_done=no" ] ;;
    *) false ;;
  esac
}

@test "a job exiting 255 stops the input but not a running sibling: 124 (GNU, BusyBox), 1 (BSD)" {
  run_lab "$HERE/exit_demo.sh" 2 "$DIR" slow die never
  case $FLAVOR in
    gnu | busybox) [ "$output" = "status=124 seen=die,slow, slow_done=yes" ] ;;
    bsd) [ "$output" = "status=1 seen=die,slow, slow_done=yes" ] ;;
    *) false ;;
  esac
}

@test "a job killed by a signal stops the input: 125 (GNU, BusyBox), 1 (BSD)" {
  run_lab "$HERE/exit_demo.sh" 1 "$DIR" kill ok ok
  case $FLAVOR in
    gnu | busybox) [ "$output" = "status=125 seen=kill, slow_done=no" ] ;;
    bsd) [ "$output" = "status=1 seen=kill, slow_done=no" ] ;;
    *) false ;;
  esac
}

@test "a missing utility is 127 under -P on every implementation" {
  run_lab_c 'echo x | "$1" -P 2 no-such-utility-xargs-p-lab 2>/dev/null' _ "${XARGS:-xargs}"
  [ "$status" -eq 127 ]
}

@test "a non-numeric -P is rejected and the utility never runs" {
  run_lab_c '"$1" -P abc echo SENTINEL-RAN </dev/null 2>&1' _ "${XARGS:-xargs}"
  [ "$status" -ne 0 ]
  case $output in
    *SENTINEL-RAN*) false ;;
    *) ;;
  esac
}

@test "--process-slot-var gives concurrent jobs distinct slots (GNU only)" {
  case $FLAVOR in
    gnu)
      run_lab_c 'seq 1 6 | "$1" -P 3 -n 1 --process-slot-var=SLOT "$2" "$3"; echo "status=$?"' _ \
        "${XARGS:-xargs}" "$HERE/slot_worker.sh" "$DIR"
      [ "$status" -eq 0 ]
      [ "$output" = "status=0" ]
      [ ! -e "$DIR/collisions.log" ]
      sort -u -o "$DIR/slots.sorted" "$DIR/slots.log"
      slots=$(tr "\n" "," <"$DIR/slots.sorted")
      uses=$(wc -l <"$DIR/slots.log")
      [ "$slots" = "0,1,2," ]
      [ "$((uses))" -eq 6 ]
      ;;
    bsd | busybox)
      run_lab_c 'echo x | "$1" -P 3 --process-slot-var=SLOT true 2>/dev/null' _ "${XARGS:-xargs}"
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "GNU reports its parallelism ceiling in --show-limits; others have no such option" {
  run_lab_c '"$1" --show-limits </dev/null 2>&1' _ "${XARGS:-xargs}"
  case $FLAVOR in
    gnu)
      [ "$status" -eq 0 ]
      case $output in
        *"Maximum parallelism (--max-procs must be no greater): 2147483647"*) ;;
        *) false ;;
      esac
      ;;
    bsd | busybox) [ "$status" -ne 0 ] ;;
    *) false ;;
  esac
}

@test "SIGUSR1 grows a GNU pool from 1 to 2; elsewhere it kills xargs" {
  run_lab "$HERE/signal_demo.sh" grow "$DIR"
  case $FLAVOR in
    gnu) [ "$output" = "killed=no peak=2" ] ;;
    bsd | busybox) [ "$output" = "killed=yes peak=1" ] ;;
    *) false ;;
  esac
}

@test "SIGUSR2 shrinks a GNU pool from 3 to 1; elsewhere it kills xargs" {
  run_lab "$HERE/signal_demo.sh" shrink "$DIR"
  case $FLAVOR in
    gnu) [ "$output" = "killed=no peak=3 late=1," ] ;;
    bsd | busybox) [ "$output" = "killed=yes peak=3 late=" ] ;;
    *) false ;;
  esac
}

@test "SIGUSR1 raises a GNU cap only when the next child exits, not when the signal arrives" {
  run_lab "$HERE/signal_demo.sh" lazy "$DIR"
  case $FLAVOR in
    gnu) [ "$output" = "killed=no peak=2 running=1" ] ;;
    bsd | busybox) [ "$output" = "killed=yes peak=1 running=1" ] ;;
    *) false ;;
  esac
}

@test "SIGUSR1 without -P: fatal in every implementation except GNU findutils before 4.10" {
  run_lab "$HERE/signal_demo.sh" plain "$DIR"
  case $FLAVOR in
    gnu)
      "${XARGS:-xargs}" --version >"$DIR/version.txt" 2>&1
      v=$(head -n 1 "$DIR/version.txt")
      v=${v##* }
      major=${v%%.*}
      rest=${v#*.}
      minor=${rest%%.*}
      if [ "$major" -gt 4 ] || { [ "$major" -eq 4 ] && [ "$minor" -ge 10 ]; }; then
        [ "$output" = "killed=yes peak=1" ]
      else
        [ "$output" = "killed=no peak=1" ]
      fi
      ;;
    bsd | busybox) [ "$output" = "killed=yes peak=1" ] ;;
    *) false ;;
  esac
}

@test "killing xargs with SIGTERM does not kill the jobs it started" {
  run_lab "$HERE/term_demo.sh" "$DIR"
  [ "$status" -eq 0 ]
  [ "$output" = "xargs_killed=yes jobs_survived=yes" ]
}

@test "one-line writes below PIPE_BUF stay whole under -P 8" {
  run_lab "$HERE/lines_demo.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "lines=800 intact=800" ]
}

@test "pasting {} into a sh -c script runs the input as code; passing it as \$1 does not" {
  mkdir "$DIR/unsafe" "$DIR/safe"
  run_lab "$HERE/inject_demo.sh" unsafe "$DIR/unsafe"
  [ "$output" = "PWNED" ]
  run_lab "$HERE/inject_demo.sh" safe "$DIR/safe"
  [ "$output" = "clean" ]
}

@test "a shell function reaches an xargs job only through bash export -f" {
  run_lab_c 'bash "$1" exported' _ "$HERE/fn_demo.bash"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'got:a\ngot:b\nstatus=0')" ]
  run_lab_c 'bash "$1" plain' _ "$HERE/fn_demo.bash"
  [ "$status" -eq 0 ]
  case $FLAVOR in
    gnu | busybox) [ "$output" = "status=123" ] ;;
    bsd) [ "$output" = "status=1" ] ;;
    *) false ;;
  esac
}
