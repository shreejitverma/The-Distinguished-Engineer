#!/bin/sh
# pane.sh: the program a minimux pane runs, under the shell being tested.
#
# Usage:
#   pane.sh repl DIR     answer one command per line typed into the pane:
#       size        what stty size reports (rows then columns)
#       cols        this shell's own COLUMNS and LINES, after it has run an external command
#       checkwinsize  in bash, turn the checkwinsize option on; other shells have no such option
#       ids         session and terminal identity of this shell (minimux ids)
#       env         the SSH_AUTH_SOCK this pane inherited
#       hex TEXT    the bytes of TEXT in hexadecimal
#       burst       wait until no client is attached, then print one line
#       quit N      exit with status N
#       anything else is printed back after "got:"
#   pane.sh signals DIR  append a line to DIR/hup on SIGHUP (then exit) and to DIR/winch on
#                        SIGWINCH, for at most 45 seconds
# MINIMUX_BIN names the minimux binary; minimux itself exports MINIMUX, the server's socket.
set -u

pane_repl() {
  stty -echo
  printf 'ready\n'
  while IFS= read -r line; do
    case $line in
      size)
        size=$(stty size)
        printf 'size:%s\n' "$size"
        ;;
      cols)
        /bin/sleep 0
        printf 'cols:%s lines:%s\n' "${COLUMNS-unset}" "${LINES-unset}"
        ;;
      checkwinsize)
        # shellcheck disable=SC3044 # shopt is reached only when the shell is bash
        if [ -n "${BASH_VERSION-}" ] && shopt -s checkwinsize; then
          printf 'checkwinsize:on\n'
        else
          printf 'checkwinsize:unavailable\n'
        fi
        ;;
      ids) "$MINIMUX_BIN" ids ;;
      env) printf 'env:%s\n' "${SSH_AUTH_SOCK-unset}" ;;
      "hex "*) "$MINIMUX_BIN" hex "${line#hex }" ;;
      burst)
        "$MINIMUX_BIN" wait "$MINIMUX" clients=0
        state=$("$MINIMUX_BIN" info "$MINIMUX" clients)
        printf 'printed-while-detached %s\n' "$state"
        ;;
      "quit "*) exit "${line#quit }" ;;
      *) printf 'got:%s\n' "$line" ;;
    esac
  done
}

pane_signals() {
  dir=$1
  trap 'echo hup >>"$dir/hup"; exit 0' HUP
  trap 'echo winch >>"$dir/winch"' WINCH
  printf 'armed\n'
  n=0
  while [ "$n" -lt 45 ]; do
    /bin/sleep 1
    n=$((n + 1))
  done
}

case $1 in
  repl) pane_repl ;;
  signals) pane_signals "$2" ;;
  *)
    printf 'pane.sh: unknown mode %s\n' "$1" >&2
    exit 2
    ;;
esac
