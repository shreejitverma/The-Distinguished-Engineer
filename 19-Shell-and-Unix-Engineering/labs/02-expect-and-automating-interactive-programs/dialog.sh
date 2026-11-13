#!/bin/sh
# dialog: interactive programs, and the expect-style dialogs that drive them.
#
# Usage:
#   dialog.sh drive DIALOG BIN WORK SHELL [SHELL_ARG...]
#       Runs under the shell being tested, with no terminal. Starts "SHELL dialog.sh inner ..."
#       (or a compiled target) through BIN/mini_expect and prints mini_expect's report:
#       a transcript= line with every byte the program wrote, then a result= line.
#       BIN holds the compiled mini_expect and targets; WORK is a scratch directory.
#   dialog.sh inner PROGRAM [ARG...]
#       The program being automated; see inner() for the list.
set -u

self=$0

# The dialogs. Each is one mini_expect command line: -x waits for exact bytes, -s sends bytes.
drive() {
  dialog=$1
  mini=$2/mini_expect
  targets=$2/targets
  work=$3
  shift 3
  case $dialog in
    # prompt, answer, prompt, answer: the canonical expect script
    login) "$mini" -x 'login: ' -s 'alice\r' -x 'Password: ' -s 's3cret\r' -x 'welcome' -- "$@" "$self" inner login ;;
    login-wrong) "$mini" -x 'login: ' -s 'alice\r' -x 'Password: ' -s 'guess\r' -x 'denied' -- "$@" "$self" inner login ;;
    # the here-document approach: both answers on a pipe, no terminal anywhere
    pipe-login) "$mini" -p -s 'alice\ns3cret\n' -- "$@" "$self" inner login ;;
    # both answers typed ahead on the terminal, before any prompt
    blind) "$mini" -s 'alice\rs3cret\r' -- "$@" "$self" inner login ;;
    blind-getpass) "$mini" -t 3 -s 'alice\rs3cret\r' -- "$@" "$self" inner login-getpass "$targets" ;;
    wait-getpass) "$mini" -x 'login: ' -s 'alice\r' -x 'Password: ' -s 's3cret\r' -x 'welcome' -- "$@" "$self" inner login-getpass "$targets" ;;
    # a terminal turns each newline into carriage return, newline
    bare-newline) "$mini" -x 'line1\nline2' -- "$@" "$self" inner lines ;;
    cr-newline) "$mini" -x 'line1\r\nline2' -- "$@" "$self" inner lines ;;
    # a prompt written in two pieces, two seconds apart
    split) "$mini" -v -x 'Password: ' -s 'x\r' -- "$@" "$self" inner slow-prompt ;;
    # the terminal echoes what was sent, so a pattern can match the driver's own input
    echo-match) "$mini" -v -x 'cmd> ' -s 'ok\r' -x 'ok' -- "$@" "$self" inner command ;;
    output-match) "$mini" -v -x 'cmd> ' -s 'ok\r' -x 'ran:ok' -- "$@" "$self" inner command ;;
    # failure paths: the pattern never comes, or the program leaves first
    timeout) "$mini" -t 2 -x 'never' -- "$@" "$self" inner hang "$work/pid" ;;
    eof) "$mini" -t 20 -x 'Password: ' -- "$@" "$self" inner early-exit ;;
    # ending the dialog: close at once (what an expect script does when it ends) or wait for end of file
    close-early) "$mini" -c -x 'go? ' -s 'y\r' -- "$@" "$self" inner finish "$work/marker" ;;
    wait-eof) "$mini" -x 'go? ' -s 'y\r' -- "$@" "$self" inner finish "$work/marker" ;;
    # a C program that prompts through stdio: on a terminal, on pipes, and on pipes without waiting
    stdio-pty) "$mini" -x 'ready\r\n' -s 'go\r' -- "$targets" stdio ;;
    stdio-pipe) "$mini" -p -t 2 -x 'ready' -- "$targets" stdio ;;
    stdio-pipe-blind) "$mini" -p -s 'go\n' -- "$targets" stdio ;;
    # the shell itself as the interactive program: match the prompt, run a command, leave with status 7
    shell)
      # shellcheck disable=SC2016 # the arithmetic expansion is typed to the driven shell, not expanded here
      HOME=$work ENV='' PS1='LAB> ' TERM=dumb BASH_SILENCE_DEPRECATION_WARNING=1 "$mini" \
        -x 'LAB> ' -s 'echo $((6*7))\r' -x '42' -x 'LAB> ' -s 'exit 7\r' -- "$@" -i
      ;;
    # the same, but the prompt is set by typing an assignment whose echo does not contain the prompt
    shell-inband)
      # shellcheck disable=SC2016 # the arithmetic expansion is typed to the driven shell, not expanded here
      HOME=$work ENV='' TERM=dumb BASH_SILENCE_DEPRECATION_WARNING=1 "$mini" \
        -s "PS1='LAB''> '\\r" -x 'LAB> ' -s 'echo $((6*7))\r' -x '42' -x 'LAB> ' -s 'exit 7\r' -- "$@" -i
      ;;
    *)
      printf 'dialog: unknown dialog %s\n' "$dialog" >&2
      return 2
      ;;
  esac
}

# The programs being automated.
inner() {
  case $1 in
    login)
      # asks for the user on stdout and stdin, and for the password on /dev/tty with echo off
      printf 'login: '
      IFS= read -r user || return 3
      if ! (: </dev/tty) 2>/dev/null; then
        printf 'no terminal for the password\n'
        return 2
      fi
      stty -echo </dev/tty
      printf 'Password: ' >/dev/tty
      IFS= read -r secret </dev/tty
      stty echo </dev/tty
      printf '\n' >/dev/tty
      check "$user" "$secret"
      ;;
    login-getpass)
      # the same, but the password prompt discards type-ahead (targets getpass)
      printf 'login: '
      IFS= read -r user || return 3
      secret=$("$2" getpass) || return 2
      check "$user" "$secret"
      ;;
    lines)
      printf 'line1\nline2\n'
      ;;
    slow-prompt)
      printf 'Pass'
      /bin/sleep 2
      printf 'word: '
      IFS= read -r secret
      printf 'got=%s\n' "$secret"
      ;;
    command)
      printf 'cmd> '
      IFS= read -r cmd
      printf 'ran:%s\n' "$cmd"
      ;;
    hang)
      # record our process ID, then become a program that never prompts
      printf '%s\n' "$$" >"$2"
      printf 'started\n'
      exec /bin/sleep 30
      ;;
    early-exit)
      printf 'bye\n'
      return 3
      ;;
    finish)
      # takes one answer, then needs two more seconds to finish its work
      printf 'go? '
      IFS= read -r answer
      /bin/sleep 2
      : >"$2"
      printf 'done %s\n' "$answer"
      ;;
    *)
      printf 'dialog: unknown program %s\n' "$1" >&2
      return 2
      ;;
  esac
}

check() {
  if [ "$1" = alice ] && [ "$2" = s3cret ]; then
    printf 'welcome %s\n' "$1"
  else
    printf 'denied\n'
    return 1
  fi
}

case ${1-} in
  drive)
    shift
    drive "$@"
    ;;
  inner)
    shift
    inner "$@"
    ;;
  *)
    printf 'usage: dialog.sh drive DIALOG BIN WORK SHELL... | inner PROGRAM [ARG...]\n' >&2
    exit 2
    ;;
esac
