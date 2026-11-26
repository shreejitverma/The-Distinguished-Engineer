#!/bin/sh
# logging-probe.sh: probe modes demonstrating logging mechanics and their shell-specific edges.
# Usage: logging-probe.sh MODE [ARG...]
# Modes: fd_clean fd_polluted fd3_log timestamp_builtin timestamp_now procsub_wait
#        append_race FILE append|trunc, tee_status, format_logfmt [LEVEL [MSG]],
#        format_json [LEVEL [MSG]], systemd_prefix LEVEL [MSG],
#        sanitize_cwe117 raw|sanitized INPUT, xtrace_separation TRACEFILE

set -u

mode=${1:?usage: logging-probe.sh MODE [ARG...]}
shift

# log_line LEVEL MSG [TS]: write one formatted log line to stderr with a single printf.
log_line() {
  ts=${3:-2026-11-15T00:00:00Z}
  printf '%s [%s] [pid:%s] %s\n' "$ts" "$1" "$$" "$2" >&2
}

# neutralize TEXT: turn newline, CR and tab into spaces and drop the other C0 controls
# (including ESC) and DEL, so one record stays on one line (CWE-117, CWE-150).
neutralize() {
  printf '%s' "$1" | tr '\n\r\t' '   ' | tr -d '\001-\010\013\014\016-\037\177'
}

# json_escape TEXT: neutralize, then escape backslash and double quote for a JSON string.
json_escape() {
  neutralize "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# logfmt_value TEXT: print TEXT bare when it is a plain token, else double-quoted with escapes.
logfmt_value() {
  case $1 in
    '' | *[!A-Za-z0-9_.:/@%+-]*)
      escaped=$(json_escape "$1")
      printf '"%s"' "$escaped"
      ;;
    *)
      printf '%s' "$1"
      ;;
  esac
}

case $mode in
  fd_clean)
    # Correct pattern: data on stdout (fd 1), diagnostics on stderr (fd 2).
    log_line INFO "starting data generation"
    printf '{"record_id":1001,"status":"active"}\n'
    printf '{"record_id":1002,"status":"pending"}\n'
    log_line INFO "completed data generation"
    ;;

  fd_polluted)
    # Anti-pattern: diagnostics on stdout, mixed into the data a downstream parser reads.
    printf '2026-11-15T00:00:00Z [INFO] starting data generation\n'
    printf '{"record_id":1001,"status":"active"}\n'
    printf '{"record_id":1002,"status":"pending"}\n'
    printf '2026-11-15T00:00:00Z [INFO] completed data generation\n'
    ;;

  fd3_log)
    # A saved copy of the original stderr keeps logging alive after fd 2 is redirected away.
    exec 3>&2
    exec 2>/dev/null
    printf 'noise-on-redirected-stderr\n' >&2
    printf 'logged-on-fd3\n' >&3
    printf 'data\n'
    ;;

  timestamp_builtin)
    # Does the printf builtin of this shell understand %(...)T (bash 4.2+, ksh93)?
    if (printf '%(%Y)T' -1 >/dev/null 2>&1); then
      if [ -n "${BASH_VERSION-}" ]; then
        res=bash_builtin
      elif [ -n "${KSH_VERSION-}" ]; then
        res=ksh_builtin
      else
        res=generic_builtin
      fi
    else
      res=external_fork
    fi
    printf 'timestamp_engine=%s\n' "$res"
    ;;

  timestamp_now)
    # One RFC 3339 UTC timestamp. The local zone is forced to EST5 so that a printf that ignored
    # the TZ override would print a time five hours behind UTC, and the test would catch it.
    # bash reads the argument -1 as "now"; ksh93 parses it as a date and prints January of the
    # current year, so ksh93 gets "now" instead (bash 4.2 has no "now" and no empty-argument form).
    TZ=EST5
    export TZ
    if [ -n "${BASH_VERSION-}" ]; then
      when=-1
    else
      when=now
    fi
    if now=$(
      TZ=UTC0
      export TZ
      printf '%(%Y-%m-%dT%H:%M:%SZ)T' "$when" 2>/dev/null
    ); then
      engine='printf'
    else
      now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
      engine='date'
    fi
    printf 'engine=%s ts=%s\n' "$engine" "$now"
    ;;

  procsub_wait)
    # Does exec 3> >(cmd) parse and run, is $! then set, and does wait on it succeed?
    # The syntax lives in eval strings so a shell without it cannot fail at parse time.
    if (eval 'exec 3> >(cat >/dev/null)') 2>/dev/null; then
      eval 'exec 3> >(cat >/dev/null)'
      set +u
      sub_pid=$!
      set -u
      exec 3>&-
      case $sub_pid in
        '') bang=empty ;;
        0) bang=zero ;;
        *) bang='set' ;;
      esac
      if wait "$sub_pid" 2>/dev/null; then
        wait_st=0
      else
        wait_st=$?
      fi
      printf 'syntax=yes bang=%s wait=%s\n' "$bang" "$wait_st"
    else
      printf 'syntax=no\n'
    fi
    ;;

  append_race)
    # Four workers each open FILE themselves, then write 100 fixed-width lines.
    # With >> (O_APPEND) every line lands; with > each worker has its own offset and they overwrite.
    file=${1:?usage: logging-probe.sh append_race FILE append|trunc}
    how=${2:?usage: logging-probe.sh append_race FILE append|trunc}
    workers=4
    lines=100
    w=1
    while [ "$w" -le "$workers" ]; do
      (
        case $how in
          append) exec 3>>"$file" ;;
          trunc) exec 3>"$file" ;;
          *) exit 2 ;;
        esac
        i=1
        while [ "$i" -le "$lines" ]; do
          printf 'w=%02d seq=%03d pad=xxxxxxxxxxxxxxxxxxxx\n' "$w" "$i" >&3
          i=$((i + 1))
        done
      ) &
      w=$((w + 1))
    done
    wait
    printf 'workers=%d lines_each=%d\n' "$workers" "$lines"
    ;;

  tee_status)
    # A failing producer piped into tee: the pipeline status is tee's unless pipefail is on.
    /bin/sh -c 'exit 7' | tee /dev/null >/dev/null
    plain=$?
    # shellcheck disable=SC3040 # whether the shell supports pipefail is the point of this probe
    if (set -o pipefail) 2>/dev/null; then
      pf=$(
        # shellcheck disable=SC3040 # only reached after the option probe succeeds
        set -o pipefail
        /bin/sh -c 'exit 7' | tee /dev/null >/dev/null
        echo "$?"
      )
    else
      pf=unsupported
    fi
    printf 'plain=%s pipefail=%s\n' "$plain" "$pf"
    ;;

  format_logfmt)
    level=${1:-INFO}
    msg=${2-"job execution finished"}
    quoted=$(logfmt_value "$msg")
    printf 'ts=2026-11-15T00:00:00Z level=%s pid=%s msg=%s\n' "$level" "$$" "$quoted"
    ;;

  format_json)
    level=${1:-INFO}
    msg=${2-"job execution finished"}
    escaped=$(json_escape "$msg")
    printf '{"ts":"2026-11-15T00:00:00Z","level":"%s","pid":%s,"msg":"%s"}\n' "$level" "$$" "$escaped"
    ;;

  systemd_prefix)
    # Map a syslog level name to its RFC 5424 severity and emit the sd-daemon(3) "<N>" prefix.
    level=${1:?usage: logging-probe.sh systemd_prefix LEVEL [MSG]}
    msg=${2:-message}
    case $level in
      emerg) p=0 ;;
      alert) p=1 ;;
      crit) p=2 ;;
      err) p=3 ;;
      warning) p=4 ;;
      notice) p=5 ;;
      info) p=6 ;;
      debug) p=7 ;;
      *)
        printf 'unknown level: %s\n' "$level" >&2
        exit 2
        ;;
    esac
    printf '<%s>%s\n' "$p" "$msg" >&2
    printf 'priority=%s\n' "$p"
    ;;

  sanitize_cwe117)
    # Untrusted input in a log line: raw lets it forge records, sanitized keeps one line.
    submode=${1:?usage: logging-probe.sh sanitize_cwe117 raw|sanitized INPUT}
    input_str=${2-}
    case $submode in
      raw)
        printf '2026-11-15T00:00:00Z [INFO] user_action=%s\n' "$input_str"
        ;;
      sanitized)
        safe_str=$(neutralize "$input_str")
        printf '2026-11-15T00:00:00Z [INFO] user_action=%s\n' "$safe_str"
        ;;
      *)
        exit 2
        ;;
    esac
    ;;

  xtrace_separation)
    # Ask for the trace on fd 4 with BASH_XTRACEFD, then run one traced command.
    # Only bash 4.1+ and BusyBox ash honor the variable; every other shell keeps tracing to stderr.
    trace_file=${1:?usage: logging-probe.sh xtrace_separation TRACEFILE}
    exec 4>"$trace_file"
    # shellcheck disable=SC3028,SC2034 # only bash and BusyBox ash read BASH_XTRACEFD
    BASH_XTRACEFD=4
    set -x
    : probe_traced_command
    set +x
    exec 4>&-
    printf 'completed=xtrace\n'
    ;;

  *)
    exit 2
    ;;
esac
