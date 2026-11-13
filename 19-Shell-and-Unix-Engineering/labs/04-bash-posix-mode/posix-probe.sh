#!/bin/sh
# posix-probe.sh: show what bash POSIX mode changes, and what other shells do natively.
#
# Usage:
#   posix-probe.sh personality  print "posix=RC plc=RC emulate=NAME" for the running shell:
#                               RC is the status of [ -o posix ] and [ -o posixlycorrect ]
#                               (0 on, 1 off or unknown option, 2 test has no -o primary),
#                               NAME is the output of zsh's emulate, or "none"
#   posix-probe.sh diff NAME    run snippet NAME and print how it ended:
#                               under bash, in a fresh default-mode bash and in a fresh
#                               POSIX-mode bash: "default=OUT/RC posix=OUT/RC";
#                               under any other shell, in a subshell: "native=OUT/RC"
#
# OUT is the snippet's standard output with each newline written as ","; stderr is discarded.
# The fresh bash processes are the same binary as the shell under test ("$BASH").

# snippet NAME: print the code of snippet NAME on standard output; fail for an unknown name.
# shellcheck disable=SC2016 # the snippets are code for the shell that runs them, expanded there
snippet() {
  case $1 in
    assign) printf '%s\n' 'x=1 :' 'echo "${x-unset}"' ;;
    exportp) printf '%s\n' 'y=1; export y' 'export -p | grep " y="' ;;
    trap) printf '%s\n' 'trap "echo hi" INT' 'trap' ;;
    tilde) printf '%s\n' 'set -- a=~/x' 'case $1 in *"~"*) echo literal ;; *) echo expanded ;; esac' ;;
    alias) printf '%s\n' 'alias pp_hello="echo aliased"' 'eval pp_hello' ;;
    errexit) printf '%s\n' 'set -e' 'x=$(false; echo survived)' 'echo "[$x]"' ;;
    toggle) printf '%s\n' 'set -o posix' 'set +o posix' 'set -e' 'x=$(false; echo survived)' 'echo "[$x]"' ;;
    dot) printf '%s\n' 'cd "$DOTDIR" || exit 9' 'o=$PATH; PATH=/nonexistent' '. lib.sh' 'PATH=$o' 'echo "${loaded-no}"' ;;
    cmdstatus) printf '%s\n' 'false' 'z=$(exit 3) w=$?' 'echo "$w"' ;;
    setfn) printf '%s\n' 'f() { :; }' 'if set | grep "^f " >/dev/null; then echo listed; else echo hidden; fi' ;;
    funcname) printf '%s\n' 'eval "unset() { echo fn; }"' 'unset x' 'echo end' ;;
    readonly) printf '%s\n' 'readonly r=1' 'r=2' 'echo survived' ;;
    badexport) printf '%s\n' 'export 1bad=x' 'echo survived' ;;
    cmdexport) printf '%s\n' 'v="a b"' 'command export e=$v' 'echo "[$e]"' ;;
    unsetpc) printf '%s\n' 'set -o posix' 'echo "${POSIXLY_CORRECT-unset}"' 'unset POSIXLY_CORRECT' 'x=1 :' 'echo "${x-unset}"' ;;
    scoped) printf '%s\n' 'f() { local - >/dev/null 2>&1; set -o posix; }' 'f' 'set -e' 'x=$(false; echo survived)' 'echo "[$x]"' ;;
    pcenv) printf '%s\n' 'set -o posix' 'env | grep "^POSIXLY_CORRECT=" || echo absent' ;;
    fnimport) printf '%s\n' 'pp_f 2>/dev/null || echo no-f' 'pp-g 2>/dev/null || echo no-g' ;;
    *) return 1 ;;
  esac
}

# flatten: copy standard input to standard output with each newline written as ",".
flatten() {
  tr '\n' ','
}

# report LABEL OUT RC: print LABEL=OUT/RC, OUT flattened.
report() {
  flat=$(printf "%s" "$2" | flatten)
  printf "%s=%s/%s" "$1" "$flat" "$3"
}

# opt_status NAME: print the exit status of [ -o NAME ] (2 where test has no -o primary).
opt_status() {
  # shellcheck disable=SC3062 # test -o is the extension being probed
  [ -o "$1" ] 2>/dev/null
  # shellcheck disable=SC2319 # the status of that test is the value reported
  printf "%s" "$?"
}

mode=${1-}
case $mode in
  personality)
    a=$(opt_status posix)
    b=$(opt_status posixlycorrect)
    e=$(emulate 2>/dev/null) || e=none
    printf 'posix=%s plc=%s emulate=%s\n' "$a" "$b" "$e"
    ;;
  diff)
    code=$(snippet "${2-}") || {
      printf 'posix-probe: unknown snippet: %s\n' "${2-}" >&2
      exit 2
    }
    DOTDIR=$(mktemp -d "${TMPDIR:-/tmp}/posix-dot.XXXXXX") || exit 2
    export DOTDIR
    printf 'loaded=yes\n' >"$DOTDIR/lib.sh"
    if [ -n "${BASH_VERSION-}" ]; then
      # A fresh process per mode, because aliases, function definitions, and fatal errors
      # act at parse time or end the shell. exec -a bash keeps a bash invoked as sh
      # (macOS /bin/sh) from forcing POSIX mode on again after +o posix.
      out=$(
        unset POSIXLY_CORRECT
        # shellcheck disable=SC3038 # only bash reaches this branch
        exec -a bash "$BASH" +o posix -c "$code" 2>/dev/null
      )
      rc=$?
      report default "$out" "$rc"
      out=$("$BASH" -o posix -c "$code" 2>/dev/null)
      rc=$?
      printf ' '
      report posix "$out" "$rc"
    else
      out=$( (eval "$code") 2>/dev/null)
      rc=$?
      report native "$out" "$rc"
    fi
    printf '\n'
    rm -f "$DOTDIR/lib.sh"
    rmdir "$DOTDIR"
    ;;
  *)
    printf 'usage: posix-probe.sh personality | diff NAME\n' >&2
    exit 2
    ;;
esac
