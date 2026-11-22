# shellcheck shell=sh
# minispec: a simplified model of the ShellSpec example runtime (ShellSpec 0.28.1).
# Sourced by a translated spec (see translate.sh); it is not ShellSpec code.
# Mechanisms modelled on purpose: one subshell per example, evaluation output captured in files,
# `call` in the current shell versus `run` in a subshell, errexit handled with the "set +e; ( ... )"
# pattern, "unhandled output" warnings, and a verdict protocol through files.
# Needs MS_WORK, an existing directory the caller owns, for the per-example files.

: "${MS_WORK:?MS_WORK must name a scratch directory}"
MS_LF=$(printf '\n.')
MS_LF=${MS_LF%.}
MS_COUNT=0
MS_FAILED=0
MS_WARNED=0

# ms_readfile VAR FILE: read a whole file into VAR, keeping trailing newlines and a missing final newline.
# A command substitution cannot do this: it strips every trailing newline. NUL bytes are not representable.
ms_readfile() {
  MS_ACC=''
  MS_PART=''
  while IFS= read -r MS_PART; do
    MS_ACC=$MS_ACC$MS_PART$MS_LF
    MS_PART=''
  done <"$2"
  eval "$1=\$MS_ACC\$MS_PART"
}

# ms_chomp: strip every trailing newline from MS_SUBJECT (a subject is compared without them).
ms_chomp() {
  while :; do
    case $MS_SUBJECT in
      *"$MS_LF") MS_SUBJECT=${MS_SUBJECT%"$MS_LF"} ;;
      *) break ;;
    esac
  done
}

# ms_example DESCRIPTION FUNCTION: run one example in its own subshell and report a verdict.
ms_example() {
  MS_COUNT=$((MS_COUNT + 1))
  MS_BASE=$MS_WORK/$MS_COUNT
  (
    set -e
    ms_reset
    "$2"
    ms_verdict
  )
  MS_RC=$?
  MS_VERDICT=abort
  if [ -f "$MS_BASE.verdict" ]; then
    read -r MS_VERDICT <"$MS_BASE.verdict"
  fi
  case $MS_VERDICT in
    pass) printf 'ok %s - %s\n' "$MS_COUNT" "$1" ;;
    warn)
      printf 'warn %s - %s\n' "$MS_COUNT" "$1"
      MS_WARNED=$((MS_WARNED + 1))
      ;;
    fail)
      printf 'not ok %s - %s\n' "$MS_COUNT" "$1"
      MS_FAILED=$((MS_FAILED + 1))
      ;;
    abort)
      printf 'not ok %s - %s (aborted: exit status %s)\n' "$MS_COUNT" "$1" "$MS_RC"
      MS_FAILED=$((MS_FAILED + 1))
      ;;
    *)
      printf 'internal error: verdict %s\n' "$MS_VERDICT" >&2
      exit 70
      ;;
  esac
  if [ -f "$MS_BASE.notes" ]; then
    while IFS= read -r MS_PART; do
      printf '  %s\n' "$MS_PART"
    done <"$MS_BASE.notes"
  fi
}

# ms_reset: per-example state, created inside the example's subshell.
ms_reset() {
  MS_IN=/dev/null
  MS_OUT=$MS_BASE.stdout
  MS_ERR=$MS_BASE.stderr
  MS_EVALUATED=''
  MS_EXPECTED=''
  MS_FAIL=''
  MS_STATUS=0
  MS_STDOUT=''
  MS_STDERR=''
  MS_UNH_STATUS=''
  MS_UNH_STDOUT=''
  MS_UNH_STDERR=''
  : >"$MS_BASE.notes"
}

# ms_note TEXT: one diagnostic line for the report.
ms_note() {
  printf '%s\n' "$1" >>"$MS_BASE.notes"
}

# ms_data TEXT: the stdin of the evaluation (without it, stdin is /dev/null so a test cannot hang).
ms_data() {
  MS_IN=$MS_BASE.stdin
  printf '%s\n' "$1" >"$MS_IN"
}

# ms_when call|run COMMAND [ARG...]: the evaluation. At most one per example.
ms_when() {
  if [ -n "$MS_EVALUATED" ]; then
    MS_FAIL=1
    ms_note 'syntax error: only one When per example'
    return 0
  fi
  MS_EVALUATED=1
  MS_KIND=$1
  shift
  case $MS_KIND in
    call)
      # Current shell: variables persist, `exit` ends the example, errexit is off inside (AND-OR list).
      "$@" >"$MS_OUT" 2>"$MS_ERR" <"$MS_IN" && :
      MS_STATUS=$?
      ;;
    run)
      # Subshell: variables are discarded, `exit` is caught, and errexit works because it is set
      # after "set +e", outside any AND-OR list.
      set +e
      (
        set -e
        "$@"
      ) >"$MS_OUT" 2>"$MS_ERR" <"$MS_IN"
      MS_STATUS=$?
      set -e
      ;;
    *)
      MS_FAIL=1
      ms_note "syntax error: unknown evaluation type $MS_KIND"
      return 0
      ;;
  esac
  ms_readfile MS_STDOUT "$MS_OUT"
  ms_readfile MS_STDERR "$MS_ERR"
  [ "$MS_STATUS" -eq 0 ] || MS_UNH_STATUS=1
  [ -z "$MS_STDOUT" ] || MS_UNH_STDOUT=1
  [ -z "$MS_STDERR" ] || MS_UNH_STDERR=1
  return 0
}

# ms_the SUBJECT should MATCHER [ARG]: the expectation. Subjects: stdout, entire stdout, stderr, status, variable NAME.
# "stdout" drops every trailing newline (ShellSpec: shellspec_chomp); "entire stdout" keeps the bytes as captured.
# Matchers: eq VALUE, be success, be failure, be undefined (variable only).
ms_the() {
  MS_EXPECTED=1
  MS_DEFINED=1
  case $1 in
    stdout | output)
      MS_SUBJECT=$MS_STDOUT
      ms_chomp
      MS_UNH_STDOUT=''
      shift
      ;;
    entire)
      if [ "$2" != stdout ]; then
        MS_FAIL=1
        ms_note "syntax error: unknown subject entire $2"
        return 0
      fi
      MS_SUBJECT=$MS_STDOUT
      MS_UNH_STDOUT=''
      shift 2
      ;;
    stderr | error)
      MS_SUBJECT=$MS_STDERR
      ms_chomp
      MS_UNH_STDERR=''
      shift
      ;;
    status)
      MS_SUBJECT=$MS_STATUS
      MS_UNH_STATUS=''
      shift
      ;;
    variable)
      eval "MS_SUBJECT=\${$2-}"
      if ! eval "[ \"\${$2+set}\" = set ]"; then
        MS_DEFINED=''
      fi
      shift 2
      ;;
    *)
      MS_FAIL=1
      ms_note "syntax error: unknown subject $1"
      return 0
      ;;
  esac
  shift # "should"
  MS_MATCHER=$1
  shift
  case $MS_MATCHER in
    eq)
      if [ -z "$MS_DEFINED" ] || [ "$MS_SUBJECT" != "$1" ]; then
        MS_FAIL=1
        ms_note "expected '$1' but got '$MS_SUBJECT'"
      fi
      ;;
    be)
      case $1 in
        success) [ "$MS_SUBJECT" = 0 ] || ms_mismatch "$1" ;;
        failure) [ "$MS_SUBJECT" != 0 ] || ms_mismatch "$1" ;;
        undefined) [ -z "$MS_DEFINED" ] || ms_mismatch "$1" ;;
        *) ms_mismatch "unknown matcher be $1" ;;
      esac
      ;;
    *) ms_mismatch "unknown matcher $MS_MATCHER" ;;
  esac
  return 0
}

ms_mismatch() {
  MS_FAIL=1
  ms_note "expectation not met: $1 (subject '$MS_SUBJECT')"
}

# ms_verdict: decide pass, warn, or fail at the end of the example and write it to the protocol file.
ms_verdict() {
  if [ -n "$MS_FAIL" ]; then
    MS_RESULT=fail
  else
    MS_RESULT=pass
    if [ -z "$MS_EXPECTED" ]; then
      MS_RESULT=warn
      ms_note 'WARNING: no expectation found'
    fi
    if [ -n "$MS_UNH_STATUS" ]; then
      MS_RESULT=warn
      ms_note "WARNING: unhandled status $MS_STATUS"
    fi
    if [ -n "$MS_UNH_STDOUT" ]; then
      MS_RESULT=warn
      ms_note 'WARNING: unhandled stdout'
    fi
    if [ -n "$MS_UNH_STDERR" ]; then
      MS_RESULT=warn
      ms_note 'WARNING: unhandled stderr'
    fi
  fi
  printf '%s\n' "$MS_RESULT" >"$MS_BASE.verdict"
}

# ms_end: print the summary and exit like ShellSpec: 101 when any example failed or warned, else 0.
ms_end() {
  printf '%s examples, %s failures, %s warnings\n' "$MS_COUNT" "$MS_FAILED" "$MS_WARNED"
  if [ $((MS_FAILED + MS_WARNED)) -gt 0 ]; then
    exit 101
  fi
  exit 0
}
