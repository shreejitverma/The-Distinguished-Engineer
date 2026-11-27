#!/usr/bin/env bats
# Tests for bounded job pools: four strategies, the status they collect, what a pool must do on
# SIGTERM, and the shell facts that decide which strategy is portable.

setup() {
  load ../lib/lab
  LABDIR="${BATS_TEST_DIRNAME}"
  POOL="${LABDIR}/pool.sh"
  EV="${BATS_TEST_TMPDIR}/events"
  : >"${EV}"
}

teardown() {
  # Kill any sleep a test left running, found through the PIDs worker.sh recorded.
  local pid
  for pid in $(awk '$1 == "+" { print $3 }' "${EV}" 2>/dev/null); do
    case $(ps -o command= -p "${pid}" 2>/dev/null) in
      *sleep*) kill "${pid}" 2>/dev/null || true ;;
      *) ;;
    esac
  done
}

# peak: the most workers running at once, from the events file.
peak() {
  "${LABDIR}/maxrun.sh" "${EV}"
}

# line_of SIGN ID: the line number in the events file of "SIGN ID" (empty if absent).
line_of() {
  awk -v s="$1" -v i="$2" '$1 == s && $2 == i { print NR; exit }' "${EV}"
}

# waitn_supported: whether the shell under test has a usable "wait -n", by feature detection.
waitn_supported() {
  run_lab_c '( /bin/sleep 0 & wait -n )'
  [ "${status}" -eq 0 ]
}

# started N: true once N workers have recorded a start (poll it with lab_poll).
started() {
  [ "$(grep -c '^+ ' "${EV}")" -ge "$1" ]
}

# gone PID: true once PID no longer runs (a zombie counts as gone).
gone() {
  kill -0 "$1" 2>/dev/null || return 0
  case $(ps -o stat= -p "$1" 2>/dev/null) in
    Z*) return 0 ;;
    *) return 1 ;;
  esac
}

@test "batch: 7 one-second workers under N=3 peak at exactly 3 and all run" {
  run_lab "${POOL}" batch 3 "${EV}" 1 1 1 1 1 1 1
  [ "${status}" -eq 0 ]
  [ "${output}" = "jobs=7 ok=7 failed=0 codes=" ]
  [ "$(peak)" -eq 3 ]
  [ "$(grep -c '^- ' "${EV}")" -eq 7 ]
}

@test "oldest: 7 one-second workers under N=3 peak at exactly 3 and all run" {
  run_lab "${POOL}" oldest 3 "${EV}" 1 1 1 1 1 1 1
  [ "${status}" -eq 0 ]
  [ "${output}" = "jobs=7 ok=7 failed=0 codes=" ]
  [ "$(peak)" -eq 3 ]
  [ "$(grep -c '^- ' "${EV}")" -eq 7 ]
}

@test "poll: 7 one-second workers under N=3 peak at exactly 3 and all run" {
  run_lab "${POOL}" poll 3 "${EV}" 1 1 1 1 1 1 1
  [ "${status}" -eq 0 ]
  [ "${output}" = "jobs=7 ok=7 failed=0 codes=" ]
  [ "$(peak)" -eq 3 ]
  [ "$(grep -c '^- ' "${EV}")" -eq 7 ]
}

@test "N=1 serializes: no two workers ever overlap, in every portable strategy" {
  local strategy
  for strategy in batch oldest poll; do
    : >"${EV}"
    run_lab "${POOL}" "${strategy}" 1 "${EV}" 0 0 0
    [ "${status}" -eq 0 ]
    [ "$(peak)" -eq 1 ]
    [ "$(grep -c '^- ' "${EV}")" -eq 3 ]
  done
}

@test "wait -n: a pool is bounded where the shell has wait -n, and refuses with status 2 where it does not" {
  if waitn_supported; then
    run_lab "${POOL}" waitn 3 "${EV}" 1 1 1 1 1 1 1
    [ "${status}" -eq 0 ]
    [ "${output}" = "jobs=7 ok=7 failed=0 codes=" ]
    [ "$(peak)" -eq 3 ]
    [ "$(grep -c '^- ' "${EV}")" -eq 7 ]
  else
    run_lab "${POOL}" waitn 3 "${EV}" 1 1 1
    [ "${status}" -eq 2 ]
    [ "${output}" = "pool.sh: this shell has no usable wait -n" ]
    [ ! -s "${EV}" ] # refused before starting any worker
  fi
}

@test "wait -n support by shell family (bash 4.3+ and BusyBox built with ASH_BASH_COMPAT; no other family)" {
  local expect
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        3.* | 4.[012].*) expect=no ;;
        *) expect=yes ;;
      esac
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh) expect=no ;;
    busybox) expect=yes ;; # build option ASH_BASH_COMPAT; on in the Debian and Alpine BusyBox 1.37.0 builds
    *) false ;;
  esac
  if waitn_supported; then
    [ "${expect}" != no ]
  else
    [ "${expect}" != yes ]
  fi
}

@test "head-of-line blocking: with a slow oldest worker, oldest and batch hold a free slot; poll and wait -n refill it" {
  # Worker 1 runs 2 s; workers 2, 3, 4 end at once. Is worker 3 started before worker 1 ends?
  run_lab "${POOL}" oldest 2 "${EV}" 2 0 0 0
  [ "${status}" -eq 0 ]
  [ "$(line_of + 3)" -gt "$(line_of - 1)" ]
  : >"${EV}"
  run_lab "${POOL}" batch 2 "${EV}" 2 0 0 0
  [ "${status}" -eq 0 ]
  [ "$(line_of + 3)" -gt "$(line_of - 1)" ]
  : >"${EV}"
  run_lab "${POOL}" poll 2 "${EV}" 2 0 0 0
  [ "${status}" -eq 0 ]
  [ "$(line_of + 3)" -lt "$(line_of - 1)" ]
  if waitn_supported; then
    : >"${EV}"
    run_lab "${POOL}" waitn 2 "${EV}" 2 0 0 0
    [ "${status}" -eq 0 ]
    [ "$(line_of + 3)" -lt "$(line_of - 1)" ]
  fi
}

@test "batch barrier: worker 1 ends at once, worker 2 runs 2 s; oldest starts worker 3 before worker 2 ends, batch after" {
  run_lab "${POOL}" oldest 2 "${EV}" 0 2 0 0
  [ "${status}" -eq 0 ]
  [ "$(line_of + 3)" -lt "$(line_of - 2)" ]
  : >"${EV}"
  run_lab "${POOL}" batch 2 "${EV}" 0 2 0 0
  [ "${status}" -eq 0 ]
  [ "$(line_of + 3)" -gt "$(line_of - 2)" ]
}

@test "every strategy reports a failed worker's exit status through wait" {
  local strategy
  for strategy in batch oldest poll; do
    : >"${EV}"
    run_lab "${POOL}" "${strategy}" 2 "${EV}" 0:3 0 0:7 0
    [ "${status}" -eq 1 ]
    case ${output} in
      "jobs=4 ok=2 failed=2 codes=3 7" | "jobs=4 ok=2 failed=2 codes=7 3") ;;
      *) false ;;
    esac
  done
  if waitn_supported; then
    : >"${EV}"
    run_lab "${POOL}" waitn 2 "${EV}" 0:3 0 0:7 0
    [ "${status}" -eq 1 ]
    case ${output} in
      "jobs=4 ok=2 failed=2 codes=3 7" | "jobs=4 ok=2 failed=2 codes=7 3") ;;
      *) false ;;
    esac
  fi
}

@test "oldest reaps in launch order, so its codes list is deterministic" {
  run_lab "${POOL}" oldest 2 "${EV}" 0:3 0 0:7 0
  [ "${status}" -eq 1 ]
  [ "${output}" = "jobs=4 ok=2 failed=2 codes=3 7" ]
}

@test "a worker killed by SIGTERM reports 128+15 (143), except ksh93 (271) and yash (399)" {
  local pool_pid pid expect
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | busybox | mksh | posh) expect=143 ;;
    ksh93) expect=271 ;;
    yash) expect=399 ;;
    *) false ;;
  esac
  lab_shell_words
  "${LAB_SHELL_WORDS[@]}" "${POOL}" oldest 1 "${EV}" hold >"${BATS_TEST_TMPDIR}/out" 2>"${BATS_TEST_TMPDIR}/err" &
  pool_pid=$!
  lab_poll 10 started 1
  pid=$(awk '$1 == "+" { print $3; exit }' "${EV}")
  kill -TERM "${pid}"
  wait "${pool_pid}" || true
  [ "$(cat "${BATS_TEST_TMPDIR}/out")" = "jobs=1 ok=0 failed=1 codes=${expect}" ]
}

@test "SIGTERM to the pool: the trap kills every running worker and the pool exits 143" {
  local strategy pool_pid pid code
  lab_shell_words
  for strategy in batch oldest poll; do
    : >"${EV}"
    "${LAB_SHELL_WORDS[@]}" "${POOL}" "${strategy}" 2 "${EV}" hold hold hold >"${BATS_TEST_TMPDIR}/out" 2>&1 &
    pool_pid=$!
    lab_poll 10 started 2
    kill -TERM "${pool_pid}"
    code=0
    wait "${pool_pid}" || code=$?
    [ "${code}" -eq 143 ]                # the pool's own exit status from its trap, not a death by signal
    [ "$(grep -c '^+ ' "${EV}")" -eq 2 ] # the third worker was never started
    for pid in $(awk '$1 == "+" { print $3 }' "${EV}"); do
      lab_poll 10 gone "${pid}"
    done
  done
}

@test "SIGTERM to a wait -n pool kills its jobs through the job table, where wait -n exists" {
  local pool_pid pid code
  if ! waitn_supported; then
    run_lab "${POOL}" waitn 2 "${EV}" hold hold hold
    [ "${status}" -eq 2 ] # refused: nothing was started, so there is nothing to terminate
    [ ! -s "${EV}" ]
    return 0
  fi
  lab_shell_words
  "${LAB_SHELL_WORDS[@]}" "${POOL}" waitn 2 "${EV}" hold hold hold >"${BATS_TEST_TMPDIR}/out" 2>&1 &
  pool_pid=$!
  lab_poll 10 started 2
  kill -TERM "${pool_pid}"
  code=0
  wait "${pool_pid}" || code=$?
  [ "${code}" -eq 143 ]
  [ "$(grep -c "^+ " "${EV}")" -eq 2 ]
  for pid in $(awk '$1 == "+" { print $3 }' "${EV}"); do
    lab_poll 10 gone "${pid}"
  done
}

@test "pool.sh rejects a bad limit and an unknown strategy with status 2" {
  run_lab "${POOL}" oldest 0 "${EV}" 0
  [ "${status}" -eq 2 ]
  run_lab "${POOL}" oldest x "${EV}" 0
  [ "${status}" -eq 2 ]
  run_lab "${POOL}" frobnicate 2 "${EV}" 0
  [ "${status}" -eq 2 ]
  [ ! -s "${EV}" ]
}

@test "a bare wait forgets a still-running job's status in bash, yash, mksh and posh; the others keep it" {
  run_lab "${LABDIR}/bare-wait.sh" direct
  [ "${status}" -eq 0 ]
  [ "${output}" = "status=3" ]
  run_lab "${LABDIR}/bare-wait.sh" bare
  [ "${status}" -eq 0 ]
  case $(lab_shell_family) in
    bash | yash | mksh | posh) [ "${output##*$'\n'}" = "status=127" ] ;;
    zsh | zsh-sh | dash | busybox | ksh93) [ "${output}" = "status=3" ] ;;
    *) false ;;
  esac
}

@test "a background job in a while-read loop keeps the loop's file as stdin in bash, zsh and ksh93, not in dash, mksh or yash" {
  local file="${BATS_TEST_TMPDIR}/in"
  run_lab "${LABDIR}/stdin-steal.sh" fg "${file}"
  [ "${status}" -eq 0 ]
  [ "${output}" = "lines=1" ] # a foreground cat drains the rest of the input, in every shell
  run_lab "${LABDIR}/stdin-steal.sh" bgnull "${file}"
  [ "${status}" -eq 0 ]
  [ "${output}" = "lines=3" ] # an explicit </dev/null on the job is the portable fix
  run_lab "${LABDIR}/stdin-steal.sh" bg "${file}"
  [ "${status}" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | ksh93) [ "${output}" = "lines=1" ] ;;
    dash | mksh | yash | busybox | posh) [ "${output}" = "lines=3" ] ;; # POSIX 2.9.3.1
    *) false ;;
  esac
}

@test "jobs-based counting is not portable: only bash and yash count a running job and drop a finished one" {
  run_lab "${LABDIR}/jobs-count.sh"
  [ "${status}" -eq 0 ]
  case $(lab_shell_family) in
    bash | yash) [ "${output}" = "before rp=2 p=2 after rp=1 p=2" ] ;;
    dash) [ "${output}" = "before rp=1 p=0 after rp=1 p=0" ] ;;
    zsh | zsh-sh) [ "${output}" = "before rp=0 p=0 after rp=0 p=0" ] ;;
    mksh) [ "${output}" = "before rp=1 p=2 after rp=1 p=1" ] ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) case ${output} in "before rp="*" p="*" after rp="*" p="*) ;; *) false ;; esac ;;
        *) [ "${output}" = "before rp=2 p=2 after rp=2 p=1" ] ;;
      esac
      ;;
    busybox | posh) case ${output} in "before rp="*" p="*" after rp="*" p="*) ;; *) false ;; esac ;;
    *) false ;;
  esac
}

@test "a pool run from the last stage of a pipeline is invisible to a later wait in bash, dash, mksh, yash, posh, BusyBox and zsh --emulate sh" {
  run_lab "${LABDIR}/pipe-subshell.sh" "${EV}"
  [ "${status}" -eq 0 ]
  case $(lab_shell_family) in
    zsh | ksh93) [ "${output}" = "waited=yes" ] ;;                                        # the last stage runs in the current shell
    bash | zsh-sh | dash | mksh | yash | busybox | posh) [ "${output}" = "waited=no" ] ;; # it runs in a subshell
    *) false ;;
  esac
}

@test "under set -e a failed wait ends the script with the job's status and strands the other workers" {
  run_lab "${LABDIR}/set-e-wait.sh"
  [ "${status}" -eq 3 ]
  [ "${output}" = "slow worker still running at exit" ]
}
