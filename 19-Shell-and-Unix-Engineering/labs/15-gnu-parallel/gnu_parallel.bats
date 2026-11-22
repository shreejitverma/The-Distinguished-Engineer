#!/usr/bin/env bats
# Tests for GNU parallel: slots, quoting, output grouping, exit status, process groups, joblog, semaphores.
# Needs GNU parallel (not moreutils parallel) on PATH; setup fails loudly if it is missing.

setup() {
  load ../lib/lab
  LABDIR="${BATS_TEST_DIRNAME}"
  GP="${LABDIR}/gp.sh"
  EV="${BATS_TEST_TMPDIR}/events"
  export PARALLEL_HOME="${BATS_TEST_TMPDIR}/home"
  export TMPDIR="${BATS_TEST_TMPDIR}"
  cd "${BATS_TEST_TMPDIR}" || return 1
  run parallel --version
  [ "${status}" -eq 0 ]
  case ${lines[0]} in
    "GNU parallel "*) ;;
    *)
      echo "GNU parallel is required; this parallel says: ${lines[0]}" >&2
      return 1
      ;;
  esac
}

teardown() {
  # Kill any background sleep a test recorded in a *.pid file; a missing or dead pid is fine.
  local f pid
  for f in "${BATS_TEST_TMPDIR}"/*.pid; do
    [ -e "${f}" ] || continue
    read -r pid <"${f}" || continue
    kill "${pid}" 2>/dev/null || true
  done
}

# gone PID: true once PID no longer runs (a zombie counts as gone).
gone() {
  kill -0 "$1" 2>/dev/null || return 0
  if [ -r "/proc/$1/status" ]; then
    awk '$1 == "State:" { exit ($2 == "Z") ? 0 : 1 }' "/proc/$1/status"
  else
    case $(ps -o stat= -p "$1" 2>/dev/null) in Z*) return 0 ;; *) return 1 ;; esac
  fi
}

@test "-jN gives N job slots: six 2 s jobs under -j3 peak at exactly 3 running" {
  : >"${EV}"
  run_lab "${GP}" -j3 "${LABDIR}/job-events.sh" "${EV}" {} ::: 1 2 3 4 5 6
  [ "${status}" -eq 0 ]
  done_count=$(grep -c '^E ' "${EV}")
  [ "${done_count}" -eq 6 ]
  run_lab "${LABDIR}/max-concurrency.sh" "${EV}"
  [ "${output}" = 3 ]
}

@test "-j1 serializes: two 2 s jobs never overlap" {
  : >"${EV}"
  run_lab "${GP}" -j1 "${LABDIR}/job-events.sh" "${EV}" {} ::: 1 2
  [ "${status}" -eq 0 ]
  run_lab "${LABDIR}/max-concurrency.sh" "${EV}"
  [ "${output}" = 1 ]
}

@test "arguments are shell-quoted but the command is not: a glob in the command expands, -q stops that" {
  mkdir globdir
  : >globdir/f1
  : >globdir/f2
  cd globdir || return 1
  run_lab "${GP}" echo '*' ::: x
  [ "${output}" = "f1 f2 x" ]
  run_lab "${GP}" -q echo '*' ::: x
  [ "${output}" = "* x" ]
  # shellcheck disable=SC2016 # a literal $HOME is the point: parallel must not let the job shell expand it
  run_lab "${GP}" -k echo '[{}]' ::: 'a b' '*' "it's" '$HOME'
  # shellcheck disable=SC2016 # a literal $HOME is the point
  [ "${output}" = "$(printf '%s\n' '[a b]' '[*]' "[it's]" '[$HOME]')" ]
}

@test "--dry-run prints the exact shell command line each job would run" {
  run_lab "${GP}" -k --dry-run echo {} ::: "it's" 'a b'
  [ "${status}" -eq 0 ]
  expected=$(printf '%s\n' "echo 'it'\"'\"'s'" "echo 'a b'")
  [ "${output}" = "${expected}" ]
}

@test "replacement strings take apart the input: {.} {/} {//} {/.}" {
  run_lab "${GP}" -k echo '{}' '{.}' '{/}' '{//}' '{/.}' ::: dir/file.tar.gz
  [ "${output}" = "dir/file.tar.gz dir/file.tar file.tar.gz dir file.tar" ]
}

@test "input is split on newline only (spaces survive) and -0 splits on NUL" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'printf "a b\nc\n" | "$1" -k echo "[{}]"' sh "${GP}"
  [ "${output}" = "$(printf '%s\n' '[a b]' '[c]')" ]
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'printf "a\nb\0c\0" | "$1" -k -0 echo "[{}]"' sh "${GP}"
  [ "${output}" = "$(printf '%s\n' '[a' 'b]' '[c]')" ]
}

@test "-n3 packs three arguments per command line" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'seq 10 | "$1" -k -n3 echo' sh "${GP}"
  [ "${output}" = "$(printf '%s\n' '1 2 3' '4 5 6' '7 8 9' '10')" ]
}

@test "empty input runs no job at all" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c ': | "$1" "echo ran >>ran.log; : {}"' sh "${GP}"
  [ "${status}" -eq 0 ]
  [ ! -e ran.log ]
}

@test "{#} and {%} are expanded by parallel, \$PARALLEL_SEQ only by the job shell" {
  # shellcheck disable=SC2016 # $PARALLEL_SEQ must reach the job shell unexpanded
  run_lab "${GP}" -j1 -k 'echo {#}:{%}:$PARALLEL_SEQ:$PARALLEL_JOBSLOT' ::: a b
  [ "${output}" = "$(printf '%s\n' 1:1:1:1 2:1:2:1)" ]
  # An unescaped $PARALLEL_SEQ is expanded by the invoking shell, where it is unset.
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '"$1" -j1 echo seq=\$PARALLEL_SEQ-{} ::: a; "$1" -j1 echo seq=$PARALLEL_SEQ-{} ::: a' sh "${GP}"
  [ "${output}" = "$(printf '%s\n' seq=1-a seq=-a)" ]
}

@test "a job does not inherit parallel's stdin" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'echo secret | "$1" -j1 "cat; echo end {}" ::: a' sh "${GP}"
  [ "${status}" -eq 0 ]
  [ "${output}" = "end a" ]
}

@test "output is grouped per job by default, mixed by -u, whole-line by --lb" {
  run_lab "${GP}" -j2 "${LABDIR}/half-line.sh" {} ::: a b
  [ "${status}" -eq 0 ]
  sorted=$(printf '%s\n' "${lines[@]}" | sort)
  [ "${sorted}" = "$(printf '%s\n' a-a b-b)" ]
  run_lab "${GP}" -j2 --lb "${LABDIR}/half-line.sh" {} ::: a b
  sorted=$(printf '%s\n' "${lines[@]}" | sort)
  [ "${sorted}" = "$(printf '%s\n' a-a b-b)" ]
  run_lab "${GP}" -j2 -u "${LABDIR}/half-line.sh" {} ::: a b
  [[ ${output} == a-b-* || ${output} == b-a-* ]]
}

@test "-k prints in input order, not completion order" {
  run_lab "${GP}" -j2 'sleep {}; echo {}' ::: 2 0
  [ "${output}" = "$(printf '%s\n' 0 2)" ]
  run_lab "${GP}" -j2 -k 'sleep {}; echo {}' ::: 2 0
  [ "${output}" = "$(printf '%s\n' 2 0)" ]
}

@test "exit status is the number of failed jobs, capped at 101" {
  run_lab "${GP}" -j4 true ::: 1 2
  [ "${status}" -eq 0 ]
  run_lab "${GP}" -j4 false ::: 1 2 3
  [ "${status}" -eq 3 ]
  args=()
  for ((i = 1; i <= 105; i++)); do args+=("${i}"); done
  run_lab "${GP}" -j8 false ::: "${args[@]}"
  [ "${status}" -eq 101 ]
}

@test "--halt now,fail=1 stops at the first failure and returns that job's status" {
  run_lab "${GP}" -j1 --halt now,fail=1 'echo ran {}; exit 7' ::: 1 2 3
  [ "${status}" -eq 7 ]
  [ "${lines[0]}" = "ran 1" ]
  [[ ${output} != *"ran 2"* ]]
  [[ ${output} != *"ran 3"* ]]
}

@test "--timeout kills the job's whole process group and the joblog records signal 15" {
  run_lab "${GP}" -j1 --timeout 1 --joblog "${BATS_TEST_TMPDIR}/log" "${LABDIR}/grandchild.sh" "${BATS_TEST_TMPDIR}/gc.pid" {} ::: x
  [ "${status}" -eq 1 ]
  read -r gc <"${BATS_TEST_TMPDIR}/gc.pid"
  lab_poll 5 gone "${gc}"
  fields=$(awk -F'\t' 'NR == 2 { print $7, $8 }' "${BATS_TEST_TMPDIR}/log")
  [ "${fields}" = "-1 15" ]
}

@test "SIGTERM to parallel kills the running job's process group, not just the shell" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '"$1" -j1 "$2" "$3" {} ::: x >/dev/null 2>&1 </dev/null & echo $! >"$4"' sh \
    "${GP}" "${LABDIR}/grandchild.sh" "${BATS_TEST_TMPDIR}/gc.pid" "${BATS_TEST_TMPDIR}/parallel.pid"
  [ "${status}" -eq 0 ]
  lab_poll 10 test -s "${BATS_TEST_TMPDIR}/gc.pid"
  read -r gc <"${BATS_TEST_TMPDIR}/gc.pid"
  read -r pp <"${BATS_TEST_TMPDIR}/parallel.pid"
  if gone "${gc}"; then
    echo "the job's child was not running before the kill" >&2
    return 1
  fi
  kill -TERM "${pp}"
  lab_poll 10 gone "${pp}"
  lab_poll 10 gone "${gc}"
}

@test "--joblog is TSV with a header, and --resume skips every Seq already logged" {
  log="${BATS_TEST_TMPDIR}/log"
  run_lab "${GP}" -j1 -k --joblog "${log}" echo run1 {} ::: a b
  [ "${output}" = "$(printf '%s\n' 'run1 a' 'run1 b')" ]
  header=$(head -n 1 "${log}")
  expected=$(printf 'Seq\tHost\tStarttime\tJobRuntime\tSend\tReceive\tExitval\tSignal\tCommand')
  [ "${header}" = "${expected}" ]
  run_lab "${GP}" -j1 -k --joblog "${log}" --resume echo run2 {} ::: a b c d
  [ "${output}" = "$(printf '%s\n' 'run2 c' 'run2 d')" ]
  seqs=$(awk -F'\t' 'NR > 1 { printf "%s ", $1 }' "${log}")
  [ "${seqs}" = "1 2 3 4 " ]
}

@test "--retry-failed reruns only the failed Seq, taking the command from the joblog" {
  log="${BATS_TEST_TMPDIR}/log"
  job='if [ {} = b ] && [ ! -e seen.b ]; then : >seen.b; exit 1; fi; echo ok {}'
  run_lab "${GP}" -j1 -k --joblog "${log}" "${job}" ::: a b c
  [ "${status}" -eq 1 ]
  [ "${output}" = "$(printf '%s\n' 'ok a' 'ok c')" ]
  run_lab "${GP}" -j1 -k --joblog "${log}" --retry-failed
  [ "${status}" -eq 0 ]
  [ "${output}" = "ok b" ]
}

@test "--semaphore with -j1 is a mutex across separate parallel invocations" {
  : >"${EV}"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '"$1" --semaphore --id mu -j1 "$2" "$3" a; "$1" --semaphore --id mu -j1 "$2" "$3" b; "$1" --semaphore --id mu --wait' sh \
    "${GP}" "${LABDIR}/job-events.sh" "${EV}"
  [ "${status}" -eq 0 ]
  order=$(tr '\n' ' ' <"${EV}")
  [ "${order}" = "S a E a S b E b " ]
}

@test "an unexported shell function is invisible to jobs; bash needs export -f and PARALLEL_SHELL=bash" {
  case $(lab_shell_family) in
    bash)
      # shellcheck disable=SC2016 # the expansions belong to the shell under test
      run_lab_c 'f() { echo fn:"$1"; }; "$1" f ::: x' sh "${GP}"
      [ "${status}" -eq 1 ]
      # shellcheck disable=SC2016 # the expansions belong to the shell under test
      run_lab_c 'f() { echo fn:"$1"; }; export -f f; PARALLEL_SHELL=bash parallel --will-cite f ::: x' sh
      [ "${status}" -eq 0 ]
      [ "${output}" = "fn:x" ]
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      # shellcheck disable=SC2016 # the expansions belong to the shell under test
      run_lab_c 'f() { echo fn:"$1"; }; "$1" f ::: x' sh "${GP}"
      [ "${status}" -eq 1 ]
      ;;
    *) false ;;
  esac
}

@test "with PARALLEL_SHELL unset, jobs run in the shell that started parallel" {
  export GP_AUTO_SHELL=1
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  probe='echo "[${BASH_VERSION:+bash}${ZSH_VERSION:+zsh}${YASH_VERSION:+yash}${POSH_VERSION:+posh}${KSH_VERSION:+ksh}${BB_ASH_VERSION:+ashbb}]"; : {}'
  run_lab "${GP}" -j1 "${probe}" ::: x
  [ "${status}" -eq 0 ]
  case $(lab_shell_family) in
    bash) [ "${output}" = "[bash]" ] ;;
    zsh | zsh-sh) [ "${output}" = "[zsh]" ] ;;
    yash) [ "${output}" = "[yash]" ] ;;
    posh) [ "${output}" = "[posh]" ] ;;
    mksh | ksh93) [ "${output}" = "[ksh]" ] ;;
    dash) [ "${output}" = "[]" ] ;;
    # UNVERIFIED guess: "busybox ash" has no ash applet name to find on some images, so parallel
    # may fall back to $SHELL; the integrator's matrix decides the exact value.
    busybox) [[ ${output} == "[ashbb]" || ${output} == "[bash]" || ${output} == "[zsh]" || ${output} == "[]" ]] ;;
    *) false ;;
  esac
}
