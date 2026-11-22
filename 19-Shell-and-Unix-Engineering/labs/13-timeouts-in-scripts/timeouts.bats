#!/usr/bin/env bats
# Timeouts in scripts: the pure-shell watchdog under every shell, read -t per shell
# family, and the timeout(1) contract per implementation (GNU, BSD and macOS, BusyBox).

setup() {
  load ../lib/lab
  WT="$BATS_TEST_DIRNAME/with-timeout.sh"
}

# build_wstat: compile the wait-status observer; a missing compiler fails the test.
build_wstat() {
  WSTAT="$BATS_TEST_TMPDIR/wstat"
  "${CC:-cc}" -O2 -o "$WSTAT" "$BATS_TEST_DIRNAME/wstat.c"
}

# timeouts_installed: print each timeout(1) command available here, one per line.
timeouts_installed() {
  local cmd
  for cmd in timeout gtimeout /usr/bin/timeout; do
    if command -v "$cmd" >/dev/null 2>&1; then
      printf '%s\n' "$cmd"
    fi
  done
  if command -v busybox >/dev/null 2>&1 && busybox timeout 1 true >/dev/null 2>&1; then
    printf '%s\n' "busybox timeout"
  fi
}

# timeout_flavor CMD...: name the implementation behind a timeout command:
# gnu, uutils, busybox, bsd (FreeBSD and macOS), or unknown.
timeout_flavor() {
  local out
  out="$("$@" --version 2>&1 </dev/null) $("$@" --help 2>&1 </dev/null)" || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"[-f | --foreground]"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# elapsed_since START: whole seconds since START (from date +%s).
elapsed_since() {
  local now
  now=$(date +%s)
  echo $((now - $1))
}

@test "with-timeout: a command that finishes in time keeps its status and output and returns at once" {
  start=$(date +%s)
  run_lab "$WT" 10 /bin/sh -c 'echo done; exit 3'
  took=$(elapsed_since "$start")
  [ "$status" -eq 3 ]
  [ "$output" = "done" ]
  [ "$took" -lt 5 ] # the cancelled watchdog must not hold the caller for its 10 s
}

@test "with-timeout: a command that outlives its deadline is killed and the status is 124" {
  start=$(date +%s)
  run_lab "$WT" 1 /bin/sleep 30
  took=$(elapsed_since "$start")
  [ "$status" -eq 124 ]
  [ "$took" -lt 6 ]
}

@test "with-timeout: a command that ignores SIGTERM is killed with SIGKILL after the grace period" {
  start=$(date +%s)
  run_lab "$WT" -k 1 1 /bin/sh -c 'trap "" TERM; exec /bin/sleep 30'
  took=$(elapsed_since "$start")
  [ "$status" -eq 124 ]
  [ "$took" -ge 2 ] # TERM at 1 s was ignored ...
  [ "$took" -lt 8 ] # ... and KILL at 2 s ended it, long before 30 s
}

@test "with-timeout: a grandchild escapes the PID-only kill and holds the output pipe open" {
  start=$(date +%s)
  run_lab "$WT" 1 /bin/sh -c '/bin/sleep 4; echo late'
  took=$(elapsed_since "$start")
  [ "$status" -eq 124 ]
  case $output in
    *late*) false ;; # the inner sh died before its echo ...
    *) true ;;
  esac
  [ "$took" -ge 3 ] # ... but its orphaned sleep kept stdout open until it exited
}

@test "with-timeout: usage errors exit 125" {
  run_lab "$WT" 1.5 /bin/true
  [ "$status" -eq 125 ]
  [ "$output" = "with-timeout: durations must be whole seconds" ]
  run_lab "$WT" 5
  [ "$status" -eq 125 ]
}

@test "read -t: a timeout ends a read that has no input, with a per-shell status" {
  # shellcheck disable=SC2016 # expanded by the shell under test
  run_lab_c '{ /bin/sleep 3; echo late; } | { read -t 1 x; echo "rc=$? x=[$x]"; }'
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        3.*) [ "$output" = "rc=1 x=[]" ] ;;
        *) [ "$output" = "rc=142 x=[]" ] ;; # 128 + SIGALRM since bash 4.0
      esac
      ;;
    mksh) [ "$output" = "rc=142 x=[]" ] ;;
    zsh | zsh-sh | ksh93 | busybox) [ "$output" = "rc=1 x=[]" ] ;;
    dash)
      [ "${lines[0]##*: }" = "Illegal option -t" ] # "sh: 1: read: Illegal option -t"
      [ "${lines[1]}" = "rc=2 x=[]" ]
      ;;
    posh | yash)
      # No -t option: the read fails at once instead of waiting for "late".
      case ${lines[$((${#lines[@]} - 1))]} in
        "rc=0 "*) false ;;
        rc=*" x=[]") true ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "read -t: what happens to a partial line when the timer expires differs by shell" {
  # shellcheck disable=SC2016 # expanded by the shell under test
  run_lab_c '{ printf part; /bin/sleep 3; echo late; } | { read -t 1 x; echo "rc=$? x=[$x]"; }'
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        3.*) [ "$output" = "rc=1 x=[]" ] ;;
        *) [ "$output" = "rc=142 x=[part]" ] ;; # bash 4.0 and later keep partial input
      esac
      ;;
    mksh) [ "$output" = "rc=142 x=[part]" ] ;;
    zsh | zsh-sh) [ "$output" = "rc=0 x=[partlate]" ] ;; # -t only waits for the first byte
    ksh93 | busybox) [ "$output" = "rc=1 x=[]" ] ;;
    dash) [ "${lines[1]}" = "rc=2 x=[]" ] ;;
    posh | yash)
      case ${lines[$((${#lines[@]} - 1))]} in
        "rc=0 "*) false ;;
        rc=*" x=[]") true ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "timeout(1): at least one implementation is installed" {
  run timeouts_installed
  [ "$status" -eq 0 ]
  [ -n "$output" ]
}

@test "timeout(1): a command that outlives the limit gives 124, except BusyBox, whose program dies of the signal" {
  build_wstat
  impls=$(timeouts_installed)
  found=0
  while IFS= read -r t; do
    found=1
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    out=$("$WSTAT" $t 1 /bin/sleep 5)
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    flavor=$(timeout_flavor $t)
    case $flavor in
      gnu | bsd | uutils) [ "$out" = "exit 124 elapsed=1" ] ;;
      busybox) [ "$out" = "signal 15 elapsed=1" ] ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$found" -eq 1 ]
}

@test "timeout(1): -p on a timeout exits 128+15 in GNU but re-raises SIGTERM in BSD and macOS" {
  build_wstat
  impls=$(timeouts_installed)
  found=0
  while IFS= read -r t; do
    found=1
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    out=$("$WSTAT" $t -p 1 /bin/sleep 5 2>/dev/null)
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    flavor=$(timeout_flavor $t)
    case $flavor in
      gnu) [ "$out" = "exit 143 elapsed=1" ] ;;
      bsd) [ "$out" = "signal 15 elapsed=1" ] ;;
      busybox) [ "$out" = "exit 1 elapsed=0" ] ;; # no -p option: usage error
      *) false ;;
    esac
  done <<<"$impls"
  [ "$found" -eq 1 ]
}

@test "timeout(1): a command killed by a signal before the limit is mirrored by timeout dying of it" {
  build_wstat
  impls=$(timeouts_installed)
  found=0
  while IFS= read -r t; do
    found=1
    # shellcheck disable=SC2086,SC2016 # two-word command; $$ belongs to the inner sh
    out=$("$WSTAT" $t 5 /bin/sh -c 'kill -TERM $$')
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    flavor=$(timeout_flavor $t)
    case $flavor in
      gnu | bsd | busybox) [ "$out" = "signal 15 elapsed=0" ] ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$found" -eq 1 ]
}

@test "timeout(1): -k sends SIGKILL to a command that ignores SIGTERM" {
  build_wstat
  impls=$(timeouts_installed)
  found=0
  while IFS= read -r t; do
    found=1
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    out=$("$WSTAT" $t -k 1 1 /bin/sh -c 'trap "" TERM; exec /bin/sleep 8')
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    flavor=$(timeout_flavor $t)
    case $flavor in
      # GNU and BSD signal their own process group, so the KILL takes timeout too.
      gnu | bsd | busybox) [ "$out" = "signal 9 elapsed=2" ] ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$found" -eq 1 ]
}

@test "timeout(1): the signal reaches grandchildren unless -f, and never in BusyBox" {
  impls=$(timeouts_installed)
  found=0
  while IFS= read -r t; do
    found=1
    start=$(date +%s)
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    out=$($t 1 /bin/sh -c '/bin/sleep 4; echo late') || true
    took=$(elapsed_since "$start")
    [ "$out" = "" ]
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    flavor=$(timeout_flavor $t)
    case $flavor in
      gnu | bsd)
        [ "$took" -lt 3 ] # the orphaned sleep died with its process group
        start=$(date +%s)
        out=$($t -f 1 /bin/sh -c '/bin/sleep 4; echo late') || true
        took=$(elapsed_since "$start")
        [ "$out" = "" ]
        [ "$took" -ge 3 ] # with -f only the inner sh is signalled
        ;;
      busybox) [ "$took" -ge 3 ] ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$found" -eq 1 ]
}

@test "timeout(1): a duration of 0 means no limit, except in BusyBox" {
  build_wstat
  impls=$(timeouts_installed)
  found=0
  while IFS= read -r t; do
    found=1
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    out=$("$WSTAT" $t 0 /bin/sleep 2)
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    flavor=$(timeout_flavor $t)
    case $flavor in
      gnu | bsd) [ "$out" = "exit 0 elapsed=2" ] ;;
      busybox)
        case $out in
          "signal 15 elapsed=0" | "signal 15 elapsed=1") true ;;
          *) false ;;
        esac
        ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$found" -eq 1 ]
}

@test "timeout(1): 127 for a missing command, 126 for one that cannot run, 125 for a bad duration" {
  build_wstat
  impls=$(timeouts_installed)
  found=0
  while IFS= read -r t; do
    found=1
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    nf=$("$WSTAT" $t 1 /no/such/command 2>/dev/null)
    # shellcheck disable=SC2086
    nx=$("$WSTAT" $t 1 "$BATS_TEST_DIRNAME/wstat.c" 2>/dev/null)
    # shellcheck disable=SC2086
    bad=$("$WSTAT" $t 1x /bin/true 2>/dev/null)
    [ "$nf" = "exit 127 elapsed=0" ]
    [ "$nx" = "exit 126 elapsed=0" ]
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    flavor=$(timeout_flavor $t)
    case $flavor in
      gnu | bsd) [ "$bad" = "exit 125 elapsed=0" ] ;;
      busybox) [ "$bad" = "exit 1 elapsed=0" ] ;;
      *) false ;;
    esac
  done <<<"$impls"
  [ "$found" -eq 1 ]
}

@test "timeout(1): a shell function is not a utility, so timeout cannot find it" {
  impls=$(timeouts_installed)
  found=0
  while IFS= read -r t; do
    found=1
    myfunc() { echo hi; }
    # shellcheck disable=SC2312 # a failed call shows up as a mismatch
    [ "$(myfunc)" = hi ] # the function exists in this shell ...
    # shellcheck disable=SC2086 # "busybox timeout" is two words
    run -127 $t 1 myfunc # ... but timeout execs, and exec searches PATH only
    [ "$status" -eq 127 ]
    # shellcheck disable=SC2086,SC2016 # the code runs in the inner shell
    run $t 1 /bin/sh -c 'myfunc() { echo hi; }; myfunc'
    [ "$status" -eq 0 ]
    [ "$output" = hi ]
  done <<<"$impls"
  [ "$found" -eq 1 ]
}
