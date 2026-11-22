# shellcheck shell=bash
# shellcheck disable=SC2119,SC2120,SC2312 # fail takes optional arguments and every helper ends in a pipeline whose last stage (fail) decides the status
# mini-assert: a small reimplementation of the mechanism behind bats-support and bats-assert.
# It is NOT those libraries; it keeps their structure so the lab can prove each mechanism:
#   - an assertion reads $status, $output and $lines from the most recent `run`
#   - on failure it prints a titled block on stderr and returns 1 (it never exits)
#   - the block is piped through a decorator that calls `cat` with the PATH saved at load time,
#     so a test that mocks `cat` on PATH cannot corrupt the diagnostic
# Load it with: BATS_LIB_PATH=<dir containing mini-assert> and `bats_load_library mini-assert`.

# Same fallback as bats-support's load.bash: bats-core >= 1.10.0 exports BATS_SAVED_PATH.
BATS_SAVED_PATH="${BATS_SAVED_PATH-$PATH}"

# mini_err [MESSAGE...]: print MESSAGE, or all of stdin, on stderr.
mini_err() {
  if [ "$#" -gt 0 ]; then
    echo "$@"
  else
    PATH="$BATS_SAVED_PATH" command cat -
  fi >&2
}

# fail [MESSAGE...]: print on stderr and return 1.
fail() {
  if [ "$#" -eq 0 ]; then mini_err; else mini_err "$@"; fi
  return 1
}

# mini_decorate TITLE: wrap stdin in a blank line, "-- TITLE --", the text, "--", a blank line.
mini_decorate() {
  echo
  echo "-- $1 --"
  PATH="$BATS_SAVED_PATH" command cat -
  echo '--'
  echo
}

# mini_count_lines TEXT: number of lines, counting a final unterminated line.
mini_count_lines() {
  local n=0 line
  while IFS= read -r line || [[ -n $line ]]; do
    n=$((n + 1))
  done < <(printf '%s' "$1")
  printf '%s\n' "$n"
}

# mini_kv WIDTH KEY VALUE [KEY VALUE...]: "key : value" columns, or "key (N lines):" blocks
# when any value has more than one line.
mini_kv() {
  local width=$1 line i multi=0
  shift
  for ((i = 2; i <= $#; i += 2)); do
    case ${!i} in *$'\n'*) multi=1 ;; *) ;; esac
  done
  while [ "$#" -gt 0 ]; do
    if [ "$multi" -eq 0 ]; then
      printf '%-*s : %s\n' "$width" "$1" "$2"
    else
      printf '%s (%d lines):\n' "$1" "$(mini_count_lines "$2")"
      while IFS= read -r line || [[ -n $line ]]; do
        printf '  %s\n' "$line"
      done < <(printf '%s' "$2")
    fi
    shift 2
  done
}

# mini_match MODE VALUE PATTERN: literal equality, substring, or bash extended regex.
mini_match() {
  case $1 in
    literal) [[ $2 == "$3" ]] ;;
    partial) [[ $2 == *"$3"* ]] ;;
    regexp) [[ $2 =~ $3 ]] ;;
    *) return 2 ;;
  esac
}

# mini_valid_regex REGEX CALLER: fail with a diagnostic when REGEX is not a valid ERE.
# A bash [[ =~ ]] with an invalid regex returns 2, a plain non-match returns 1.
mini_valid_regex() {
  local rc=0
  # shellcheck disable=SC2319 # $? is read in the same list as the [[ it follows
  [[ '' =~ $1 ]] 2>/dev/null || rc=$?
  if [ "$rc" -eq 2 ]; then
    echo "Invalid extended regular expression: \`$1'" | mini_decorate "ERROR: $2" | fail
    return 1
  fi
}

# assert COMMAND...: fail unless COMMAND succeeds (a simple command, not [[ ]]).
assert() {
  if ! "$@"; then
    mini_kv 10 expression "$*" | mini_decorate 'assertion failed' | fail
  fi
}

# assert_equal ACTUAL EXPECTED
assert_equal() {
  if [[ $1 != "$2" ]]; then
    mini_kv 8 expected "$2" actual "$1" | mini_decorate 'values do not equal' | fail
  fi
}

# assert_success: fail unless the last `run` had status 0.
assert_success() {
  : "${output?}"
  : "${status?}"
  if ((status != 0)); then
    {
      mini_kv 6 status "$status"
      mini_kv 6 output "$output"
      if [[ -n ${stderr-} ]]; then mini_kv 6 stderr "$stderr"; fi
    } | mini_decorate 'command failed' | fail
  fi
}

# assert_failure [STATUS]: fail unless the last `run` had a nonzero status (or exactly STATUS).
assert_failure() {
  : "${output?}"
  : "${status?}"
  if ((status == 0)); then
    mini_kv 6 output "$output" | mini_decorate 'command succeeded, but it was expected to fail' | fail
  elif [ "$#" -gt 0 ] && ((status != $1)); then
    {
      mini_kv 8 expected "$1" actual "$status"
      mini_kv 8 output "$output"
    } | mini_decorate 'command failed as expected, but status differs' | fail
  fi
}

# assert_output [-p|-e] [--] [EXPECTED]: compare $output; no arguments means "non-empty".
assert_output() {
  : "${output?}"
  local mode=literal nonempty=0
  [ "$#" -gt 0 ] || nonempty=1
  while [ "$#" -gt 0 ]; do
    case $1 in
      -p | --partial) mode=partial ;;
      -e | --regexp) mode=regexp ;;
      --)
        shift
        break
        ;;
      *) break ;;
    esac
    shift
  done
  local expected=${1-}
  if [ "$nonempty" -eq 1 ]; then
    if [ -z "$output" ]; then
      echo 'expected non-empty output, but output was empty' | mini_decorate 'no output' | fail
    fi
    return
  fi
  if [ "$mode" = regexp ]; then
    mini_valid_regex "$expected" assert_output || return 1
  fi
  mini_match "$mode" "$output" "$expected" && return 0
  case $mode in
    literal) mini_kv 8 expected "$expected" actual "$output" | mini_decorate 'output differs' | fail ;;
    partial) mini_kv 9 substring "$expected" output "$output" | mini_decorate 'output does not contain substring' | fail ;;
    regexp) mini_kv 6 regexp "$expected" output "$output" | mini_decorate 'regular expression does not match output' | fail ;;
    *) return 2 ;;
  esac
}

# refute_output [-p|-e] [--] [UNEXPECTED]: the complement; no arguments means "empty".
refute_output() {
  : "${output?}"
  local mode=literal empty=0
  [ "$#" -gt 0 ] || empty=1
  while [ "$#" -gt 0 ]; do
    case $1 in
      -p | --partial) mode=partial ;;
      -e | --regexp) mode=regexp ;;
      --)
        shift
        break
        ;;
      *) break ;;
    esac
    shift
  done
  local unexpected=${1-}
  if [ "$empty" -eq 1 ]; then
    if [ -n "$output" ]; then
      mini_kv 6 output "$output" | mini_decorate 'output non-empty, but expected no output' | fail
    fi
    return
  fi
  if [ "$mode" = regexp ]; then
    mini_valid_regex "$unexpected" refute_output || return 1
  fi
  if mini_match "$mode" "$output" "$unexpected"; then
    mini_kv 6 output "$output" | mini_decorate "output matches ($mode), but it was expected not to" | fail
  fi
}

# assert_line [-n INDEX] [-p|-e] [--] EXPECTED: compare one element of $lines, or search all of them.
assert_line() {
  : "${lines?}"
  local mode=literal idx=
  while [ "$#" -gt 0 ]; do
    case $1 in
      -n | --index)
        idx=$2
        shift
        ;;
      -p | --partial) mode=partial ;;
      -e | --regexp) mode=regexp ;;
      --)
        shift
        break
        ;;
      *) break ;;
    esac
    shift
  done
  local expected=$1 line
  if [ "$mode" = regexp ]; then
    mini_valid_regex "$expected" assert_line || return 1
  fi
  if [ -n "$idx" ]; then
    mini_match "$mode" "${lines[$idx]-}" "$expected" && return 0
    mini_kv 8 index "$idx" expected "$expected" actual "${lines[$idx]-}" | mini_decorate 'line differs' | fail
    return
  fi
  for line in "${lines[@]}"; do
    mini_match "$mode" "$line" "$expected" && return 0
  done
  mini_kv 8 line "$expected" output "$output" | mini_decorate 'output does not contain line' | fail
}
