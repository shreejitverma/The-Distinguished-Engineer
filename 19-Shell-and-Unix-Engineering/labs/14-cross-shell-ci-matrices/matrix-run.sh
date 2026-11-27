#!/bin/sh
# matrix-run.sh: run one script under every shell in a list (the matrix cells) and summarize.
#
# Usage: matrix-run.sh [-a] [-i] [-v] [-E NAME=VALUE]... [-e EXPECT] -s LABEL=COMMAND... SCRIPT [ARG...]
#
#   -s LABEL=COMMAND  one cell; COMMAND is a shell plus optional arguments, split on spaces only
#                     (for example "busybox ash"); LABEL is [A-Za-z0-9._-]+ and names the cell's files
#   -i                hermetic: start every cell from an empty environment holding only PATH
#   -E NAME=VALUE     add a variable to every cell's environment (after the -i scrub)
#   -e EXPECT         the standard output every cell must produce, byte for byte
#   -a                a cell whose shell is not installed is allowed (reported, not fatal)
#   -v                print each cell's standard output
#
# A cell is pass (exit 0, output matches -e if given), fail (non-zero exit), differ (exit 0 but the
# output differs from -e), or absent (the shell is not installed).
# Exit status: 0 every cell passed; 1 a cell failed or differed; 2 usage error;
# 3 a cell was absent without -a, or no cell ran at all (a matrix that ran nothing is not green).
# Cells run one after another with standard input from /dev/null; there is no per-cell timeout here
# (CI bounds the job with timeout-minutes, and bats bounds a test with BATS_TEST_TIMEOUT).
#
# POSIX sh only, and no array or local: the driver must itself run under every shell it drives.

die() {
  printf 'matrix-run: %s\n' "$*" >&2
  exit 2
}

# run_cell LABEL COMMAND SCRIPT [ARG...]: run one cell, set m_state and m_rc.
run_cell() {
  m_label=$1
  m_cmd=$2
  shift 2
  m_nargs=$#
  m_first=${m_cmd%% *}
  m_found=$(command -v "$m_first") || m_found=
  if [ -z "$m_found" ] || [ ! -x "$m_found" ]; then
    m_state=absent
    m_rc=-
    return 0
  fi
  # Append the shell words after the script arguments, then rotate the script arguments behind them.
  m_rest=$m_cmd
  while [ -n "$m_rest" ]; do
    case $m_rest in
      *" "*)
        m_word=${m_rest%% *}
        m_rest=${m_rest#* }
        ;;
      *)
        m_word=$m_rest
        m_rest=
        ;;
    esac
    if [ -n "$m_word" ]; then
      set -- "$@" "$m_word"
    fi
  done
  m_i=0
  while [ "$m_i" -lt "$m_nargs" ]; do
    set -- "$@" "$1"
    shift
    m_i=$((m_i + 1))
  done
  # Replace the shell word by its resolved path, so env's own PATH lookup cannot pick another shell.
  shift
  set -- "$m_found" "$@"
  # Prepend the env options: append them, then rotate the whole command behind them.
  m_ncmd=$#
  if [ "$m_hermetic" -eq 1 ]; then
    set -- "$@" -i "PATH=$PATH"
  fi
  if [ -s "$m_work/envs" ]; then
    while IFS= read -r m_item; do
      set -- "$@" "$m_item"
    done <"$m_work/envs"
  fi
  m_i=0
  while [ "$m_i" -lt "$m_ncmd" ]; do
    set -- "$@" "$1"
    shift
    m_i=$((m_i + 1))
  done
  if env "$@" </dev/null >"$m_work/$m_label.out" 2>"$m_work/$m_label.err"; then
    m_rc=0
  else
    m_rc=$?
  fi
  if [ "$m_rc" -ne 0 ]; then
    m_state=fail
  elif [ -n "$m_expect" ] && ! cmp -s "$m_expect" "$m_work/$m_label.out"; then
    m_state=differ
  else
    m_state=pass
  fi
}

allow_absent=0
m_hermetic=0
m_verbose=0
m_expect=
m_work=$(mktemp -d "${TMPDIR:-/tmp}/matrix-run.XXXXXX") || die "mktemp failed"
trap 'rm -rf "$m_work"' EXIT
trap 'exit 1' HUP INT TERM
: >"$m_work/cells"
: >"$m_work/envs"

while getopts aivE:e:s: m_opt; do
  case $m_opt in
    a) allow_absent=1 ;;
    i) m_hermetic=1 ;;
    v) m_verbose=1 ;;
    E)
      case $OPTARG in
        [A-Za-z_]*=*) printf '%s\n' "$OPTARG" >>"$m_work/envs" ;;
        *) die "-E wants NAME=VALUE, not '$OPTARG'" ;;
      esac
      ;;
    e)
      [ -r "$OPTARG" ] || die "cannot read the expected-output file '$OPTARG'"
      m_expect=$OPTARG
      ;;
    s)
      case $OPTARG in
        *=*) ;;
        *) die "-s wants LABEL=COMMAND, not '$OPTARG'" ;;
      esac
      case ${OPTARG%%=*} in
        '' | *[!A-Za-z0-9._-]*) die "a cell label must match [A-Za-z0-9._-]+, not '${OPTARG%%=*}'" ;;
        *) ;;
      esac
      [ -n "${OPTARG#*=}" ] || die "cell '${OPTARG%%=*}' has an empty command"
      printf '%s\n' "$OPTARG" >>"$m_work/cells"
      ;;
    *) die "unknown option (see the header of $0)" ;;
  esac
done
shift $((OPTIND - 1))
[ "$#" -ge 1 ] || die "no script to run"
[ -s "$m_work/cells" ] || die "no cell given (use -s LABEL=COMMAND)"

n_pass=0
n_bad=0
n_absent=0
while IFS= read -r m_spec; do
  m_cell=${m_spec%%=*}
  run_cell "$m_cell" "${m_spec#*=}" "$@"
  printf '%-14s %-6s rc=%s\n' "$m_cell" "$m_state" "$m_rc"
  case $m_state in
    pass) n_pass=$((n_pass + 1)) ;;
    absent) n_absent=$((n_absent + 1)) ;;
    *) n_bad=$((n_bad + 1)) ;;
  esac
  if [ "$m_state" = fail ]; then
    sed 's/^/    stderr: /' "$m_work/$m_cell.err"
  elif [ "$m_state" = differ ]; then
    diff "$m_expect" "$m_work/$m_cell.out" | sed 's/^/    diff: /'
  fi
  if [ "$m_verbose" -eq 1 ] && [ "$m_state" != absent ]; then
    sed 's/^/    out: /' "$m_work/$m_cell.out"
  fi
done <"$m_work/cells"

printf 'matrix: %d pass, %d fail, %d absent\n' "$n_pass" "$n_bad" "$n_absent"
if [ "$n_bad" -gt 0 ]; then
  exit 1
fi
if [ $((n_pass + n_bad)) -eq 0 ]; then
  exit 3
fi
if [ "$n_absent" -gt 0 ] && [ "$allow_absent" -eq 0 ]; then
  exit 3
fi
exit 0
