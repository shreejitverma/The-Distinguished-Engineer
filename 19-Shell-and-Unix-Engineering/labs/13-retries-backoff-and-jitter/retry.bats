#!/usr/bin/env bats
# Tests for retry.sh (capped exponential backoff with jitter) and herd.sh (where jitter comes from).

setup() {
  load ../lib/lab
  RETRY="$BATS_TEST_DIRNAME/retry.sh"
  FLAKY="$BATS_TEST_DIRNAME/flaky.sh"
  HERD="$BATS_TEST_DIRNAME/herd.sh"
  COUNT="$BATS_TEST_TMPDIR/count"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

# assert_calls N: flaky.sh ran exactly N times.
assert_calls() {
  local n=0
  if [ -f "$COUNT" ]; then
    n=$(wc -l <"$COUNT")
  fi
  [ $((n)) -eq "$1" ]
}

# assert_lines FILE N: FILE has exactly N lines.
assert_lines() {
  local n
  n=$(wc -l <"$1")
  [ $((n)) -eq "$2" ]
}

# plan_delays: print only the millisecond values from a plan in $output.
plan_delays() {
  printf '%s\n' "$output" | awk '{ print $3 }'
}

@test "without jitter the plan doubles from the base and stops at the cap" {
  run_lab "$RETRY" -p -n 6 -b 1000 -c 8000 -j none -- true
  [ "$status" -eq 0 ]
  [ "$output" = "retry 1: 1000 ms
retry 2: 2000 ms
retry 3: 4000 ms
retry 4: 8000 ms
retry 5: 8000 ms" ]
}

@test "the doubling stops at the cap instead of overflowing after 70 retries" {
  run_lab "$RETRY" -p -n 71 -b 1000 -c 600000 -j none -- true
  [ "$status" -eq 0 ]
  line70=$(printf '%s\n' "$output" | sed -n 70p)
  [ "$line70" = "retry 70: 600000 ms" ]
}

@test "full jitter draws each delay from 0 to the exponential ceiling, not the ceiling itself" {
  run_lab "$RETRY" -p -n 41 -b 1000 -c 8000 -j full -- true
  [ "$status" -eq 0 ]
  plan_delays >"$BATS_TEST_TMPDIR/d"
  assert_lines "$BATS_TEST_TMPDIR/d" 40
  # Retry k has ceiling min(8000, 1000 * 2^(k-1)); every draw must lie in [0, ceiling].
  run awk 'BEGIN { bad = 0 } { c = 1000 * 2 ^ (NR - 1); if (c > 8000) c = 8000; if ($1 < 0 || $1 > c) bad++; if ($1 != c) spread++ } END { print bad, (spread > 30) }' "$BATS_TEST_TMPDIR/d"
  [ "$output" = "0 1" ]
}

@test "equal jitter keeps at least half of the ceiling" {
  run_lab "$RETRY" -p -n 41 -b 1000 -c 8000 -j equal -- true
  [ "$status" -eq 0 ]
  plan_delays >"$BATS_TEST_TMPDIR/d"
  run awk '{ c = 1000 * 2 ^ (NR - 1); if (c > 8000) c = 8000; if ($1 < c / 2 || $1 > c) bad++; if ($1 != c) spread++ } END { print bad + 0, (spread > 30) }' "$BATS_TEST_TMPDIR/d"
  [ "$output" = "0 1" ]
}

@test "decorrelated jitter stays between the base and three times the previous delay, capped" {
  run_lab "$RETRY" -p -n 41 -b 1000 -c 8000 -j decorr -- true
  [ "$status" -eq 0 ]
  plan_delays >"$BATS_TEST_TMPDIR/d"
  run awk 'BEGIN { prev = 1000 } { hi = 3 * prev; if (hi > 8000) hi = 8000; if ($1 < 1000 || $1 > hi) bad++; seen[$1] = 1; prev = $1 } END { n = 0; for (v in seen) n++; print bad + 0, (n > 10) }' "$BATS_TEST_TMPDIR/d"
  [ "$output" = "0 1" ]
}

@test "decorrelated jitter with a zero base is stuck at zero" {
  run_lab "$RETRY" -p -n 4 -b 0 -c 8000 -j decorr -- true
  [ "$status" -eq 0 ]
  [ "$output" = "retry 1: 0 ms
retry 2: 0 ms
retry 3: 0 ms" ]
}

@test "without /dev/urandom the awk fallback still spreads the draws" {
  RETRY_URANDOM="$BATS_TEST_TMPDIR/missing" run_lab "$RETRY" -p -n 11 -b 100000 -c 100000 -j full -- true
  [ "$status" -eq 0 ]
  plan_delays >"$BATS_TEST_TMPDIR/d"
  assert_lines "$BATS_TEST_TMPDIR/d" 10
  run awk '$1 < 0 || $1 > 100000 { bad++ } { seen[$1] = 1 } END { n = 0; for (v in seen) n++; print bad + 0, (n >= 5) }' "$BATS_TEST_TMPDIR/d"
  [ "$output" = "0 1" ]
}

@test "a transient failure is retried until the command succeeds" {
  run_lab "$RETRY" -n 5 -b 0 -c 0 -- "$FLAKY" "$COUNT" 2 1
  [ "$status" -eq 0 ]
  assert_calls 3
  [ "$output" = "retry: attempt 1 failed with status 1; retrying in 0 ms
retry: attempt 2 failed with status 1; retrying in 0 ms" ]
}

@test "when attempts run out the command's own status comes back, not 1" {
  run_lab "$RETRY" -n 3 -b 0 -c 0 -- "$FLAKY" "$COUNT" 10 75
  [ "$status" -eq 75 ]
  assert_calls 3
  [[ "$output" == *"attempt 3 failed with status 75; giving up after 3 attempts" ]]
}

@test "a status outside the -r list is permanent and is not retried" {
  run_lab "$RETRY" -n 5 -b 0 -c 0 -r "75" -- "$FLAKY" "$COUNT" 10 1
  [ "$status" -eq 1 ]
  assert_calls 1
  [ "$output" = "retry: attempt 1 failed with status 1, which is not retryable" ]
}

@test "a status inside the -r list is retried" {
  run_lab "$RETRY" -n 5 -b 0 -c 0 -r "69 75" -- "$FLAKY" "$COUNT" 1 75
  [ "$status" -eq 0 ]
  assert_calls 2
}

@test "command not found (127) is permanent: a typo is never transient" {
  run_lab "$RETRY" -n 5 -b 0 -c 0 -- "$BATS_TEST_TMPDIR/no-such-command"
  [ "$status" -eq 127 ]
  [[ "$output" == *"attempt 1 failed with status 127, which is not retryable" ]]
}

@test "status 255 (ssh's connection failure) is retried, not mistaken for a signal" {
  run_lab "$RETRY" -n 5 -b 0 -c 0 -- "$FLAKY" "$COUNT" 1 255
  [ "$status" -eq 0 ]
  assert_calls 2
}

@test "a command killed by SIGINT stops the loop; ksh93u+ 2012 dies of SIGINT itself before the wrapper logs" {
  run_lab "$RETRY" -n 5 -b 0 -c 0 -- "$FLAKY" "$COUNT" 10 INT
  [ "$status" -eq 130 ]
  assert_calls 1
  family=$(lab_shell_family)
  # ksh93u+m 1.0.10 on Linux logs like the other shells (measured in the Debian image).
  if [ "$family" = ksh93 ]; then
    case $(lab_shell_version) in
      *93u+m*) family=ksh93u+m ;;
      *) ;;
    esac
  fi
  case $family in
    # macOS ksh93u+ 2012: job_wait() propagates the child's SIGINT to the shell itself
    ksh93) [ -z "$output" ] ;;
    bash | zsh | zsh-sh | dash | ksh93u+m | mksh | yash | busybox | posh)
      [ "$output" = "retry: attempt 1 was killed by signal 2; not retrying" ]
      ;;
    *) false ;;
  esac
}

@test "a real backoff sleep happens between attempts" {
  t0=$(date +%s)
  run_lab "$RETRY" -n 2 -b 1000 -c 1000 -j none -- "$FLAKY" "$COUNT" 1 1
  t1=$(date +%s)
  [ "$status" -eq 0 ]
  assert_calls 2
  [ $((t1 - t0)) -ge 1 ]
}

@test "the deadline stops the loop before a sleep that would overrun it" {
  t0=$(date +%s)
  run_lab "$RETRY" -n 5 -b 4000 -c 4000 -j none -d 2 -- "$FLAKY" "$COUNT" 10 1
  t1=$(date +%s)
  [ "$status" -eq 1 ]
  assert_calls 1
  [ "$output" = "retry: attempt 1 failed with status 1; a 4000 ms sleep would pass the 2s deadline" ]
  [ $((t1 - t0)) -lt 3 ]
}

@test "usage errors exit 2: no command, an unknown jitter, a cap below the base" {
  run_lab "$RETRY" -n 3
  [ "$status" -eq 2 ]
  run_lab "$RETRY" -j wobbly -- true
  [ "$status" -eq 2 ]
  run_lab "$RETRY" -b 5000 -c 1000 -- true
  [ "$status" -eq 2 ]
  run_lab "$RETRY" -r "1,75" -- true
  [ "$status" -eq 2 ]
}

@test "a herd of workers drawing from /dev/urandom spreads out" {
  run_lab "$HERD" urandom 20
  [ "$status" -eq 0 ]
  k=${output#distinct=}
  k=${k%% *}
  [ "$k" -ge 18 ]
}

@test "awk srand() seeds from the clock: the herd gets one or two values" {
  run_lab "$HERD" awk 20
  [ "$status" -eq 0 ]
  k=${output#distinct=}
  k=${k%% *}
  case $(lab_awk_flavor) in
    gawk | onetrue | busybox) [ "$k" -le 2 ] ;; # srand() uses time(): whole seconds
    # mawk 1.3.4 20250131 seeds from nanoseconds. On aarch64 (Debian image) the (unsigned) cast of a
    # double above UINT_MAX saturates, so every clock seed collapses to one value: 1 of 20 measured.
    # On x86-64 the cast wraps and the herd spreads: 20 of 20 measured.
    mawk)
      case $(uname -m) in
        aarch64 | arm64) [ "$k" -le 2 ] ;;
        *) [ "$k" -ge 18 ] ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "awk srand() with no argument sets the seed to the epoch second" {
  checked=0
  awks=$(lab_awks)
  while IFS= read -r cmd; do
    # shellcheck disable=SC2086,SC2248 # cmd is "awk" or "busybox awk" and must split into words
    seed=$($cmd 'BEGIN { srand(); printf "%d\n", srand() }')
    now=$(date +%s)
    # shellcheck disable=SC2086,SC2248 # same split as above
    flavor=$(lab_awk_flavor $cmd)
    case $flavor in
      gawk | onetrue | busybox) [ $((now - seed)) -ge 0 ] && [ $((now - seed)) -le 1 ] ;;
      mawk) [ "$seed" -gt $((now * 1000)) ] ;; # 1.3.4 seeds from the nanosecond clock
      *) false ;;
    esac
    checked=$((checked + 1))
  done <<<"$awks"
  [ "$checked" -ge 1 ]
}

@test "the shell's own RANDOM in forked workers: zsh and yash before 2.62 repeat one value, dash and posh have none" {
  run_lab "$HERD" random 20
  family=$(lab_shell_family)
  # yash reseeds $RANDOM per subshell only from 2.62 (NEWS); 2.58.1 repeats (measured on Alpine).
  if [ "$family" = yash ]; then
    case $(lab_shell_version) in
      2.[0-5]* | 2.60* | 2.61*) family=yash-old ;;
      *) ;;
    esac
  fi
  case $family in
    zsh | zsh-sh | yash-old)
      [ "$status" -eq 0 ]
      [ "$output" = "distinct=1 of 20" ]
      ;;
    bash | ksh93 | mksh | yash | busybox)
      [ "$status" -eq 0 ]
      k=${output#distinct=}
      k=${k%% *}
      [ "$k" -ge 18 ]
      ;;
    dash | posh)
      [ "$status" -eq 3 ]
      [ "$output" = "RANDOM is unset" ]
      ;;
    *) false ;;
  esac
}
