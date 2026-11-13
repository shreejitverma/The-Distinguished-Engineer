#!/usr/bin/env bats
# Tests for the expect loop: mini_expect.c drives the programs of dialog.sh and targets.c on a
# pseudo-terminal, with the shell under test both running the driver script and being the program.

setup() {
  load ../lib/lab
  SCRIPT="${BATS_TEST_DIRNAME}/dialog.sh"
  BIN="${BATS_TEST_TMPDIR}/bin"
  WORK="${BATS_TEST_TMPDIR}/work"
  mkdir "${BIN}" "${WORK}"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror "${BATS_TEST_DIRNAME}/mini_expect.c" -o "${BIN}/mini_expect"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror "${BATS_TEST_DIRNAME}/targets.c" -o "${BIN}/targets"
}

# dialog NAME [SHELL_FLAG...]: run one dialog of dialog.sh under the shell under test, which is
# also the shell that runs the program being driven.
dialog() {
  local name=$1
  shift
  lab_shell_words
  run_lab "${SCRIPT}" drive "${name}" "${BIN}" "${WORK}" "${LAB_SHELL_WORDS[@]}" "$@"
}

# echoes_twice: true for ksh93u+ 2012 on a BSD-derived terminal driver (macOS /bin/ksh), whose
# read toggles ICANON, so the driver retypes and echoes the pending line a second time.
echoes_twice() {
  local family version
  family=$(lab_shell_family)
  version=$(lab_shell_version)
  [ "${family}" = ksh93 ] || return 1
  case ${version} in
    *93u+m*) return 1 ;;
    *) [ ! -d /dev/pts ] ;;
  esac
}

# libc_flavor: the C library the targets are linked against, asked of the compiler: glibc, musl, or apple.
libc_flavor() {
  local machine
  machine=$("${CC:-cc}" -dumpmachine)
  case ${machine} in
    *musl*) printf 'musl\n' ;;
    *gnu*) printf 'glibc\n' ;;
    *apple* | *darwin*) printf 'apple\n' ;;
    *) printf 'unknown\n' ;;
  esac
}

@test "answers on a pipe never reach a prompt that reads /dev/tty: with no terminal the program gives up" {
  dialog pipe-login
  [ "${status}" -eq 2 ]
  # shellcheck disable=SC2016 # mini_expect's escapes, not shell syntax
  expect='transcript=login: no terminal for the password\n
result=ok status=2'
  [ "${output}" = "${expect}" ]
}

@test "on a pty the dialog completes: the user name is echoed, the password is not, the status is 0" {
  dialog login
  [ "${status}" -eq 0 ]
  if echoes_twice; then
    # shellcheck disable=SC2016 # mini_expect's escapes
    expect='transcript=login: alice\r\nalice\r\nPassword: \r\nwelcome alice\r\n
result=ok status=0'
  else
    # shellcheck disable=SC2016 # mini_expect's escapes
    expect='transcript=login: alice\r\nPassword: \r\nwelcome alice\r\n
result=ok status=0'
  fi
  [ "${output}" = "${expect}" ]
}

@test "the program's exit status comes back through the driver: a wrong password gives 1" {
  dialog login-wrong
  [ "${status}" -eq 1 ]
  [ "${lines[1]}" = "result=ok status=1" ]
  case ${lines[0]} in
    *'Password: \r\ndenied\r\n') ;;
    *) false ;;
  esac
  case ${lines[0]} in
    *guess*) false ;;
    *) ;;
  esac
}

@test "answers typed ahead reach a prompt that keeps the input queue, and the password is echoed" {
  dialog blind
  if echoes_twice; then
    # macOS /bin/ksh: the stty that follows ksh's read drops the line still pending in the terminal
    [ "${status}" -eq 124 ]
    # shellcheck disable=SC2016 # mini_expect's escapes
    expect='transcript=alice\r\ns3cret\r\nlogin: alice\r\nPassword: 
result=timeout waiting=eof'
  else
    # the secret arrived while echo was still on, so the terminal printed it
    [ "${status}" -eq 0 ]
    # shellcheck disable=SC2016 # mini_expect's escapes
    expect='transcript=alice\r\ns3cret\r\nlogin: Password: \r\nwelcome alice\r\n
result=ok status=0'
  fi
  [ "${output}" = "${expect}" ]
}

@test "answers typed ahead are discarded by a prompt that flushes input, as getpass does: the dialog times out" {
  dialog blind-getpass
  [ "${status}" -eq 124 ]
  if echoes_twice; then
    # shellcheck disable=SC2016 # mini_expect's escapes
    expect='transcript=alice\r\ns3cret\r\nlogin: alice\r\nPassword: 
result=timeout waiting=eof'
  else
    # shellcheck disable=SC2016 # mini_expect's escapes
    expect='transcript=alice\r\ns3cret\r\nlogin: Password: 
result=timeout waiting=eof'
  fi
  [ "${output}" = "${expect}" ]
}

@test "waiting for each prompt before answering works against the flushing prompt" {
  dialog wait-getpass
  [ "${status}" -eq 0 ]
  if echoes_twice; then
    # shellcheck disable=SC2016 # mini_expect's escapes
    expect='transcript=login: alice\r\nalice\r\nPassword: \r\nwelcome alice\r\n
result=ok status=0'
  else
    # shellcheck disable=SC2016 # mini_expect's escapes
    expect='transcript=login: alice\r\nPassword: \r\nwelcome alice\r\n
result=ok status=0'
  fi
  [ "${output}" = "${expect}" ]
}

@test "a pattern with a bare newline never matches terminal output; carriage return, newline does" {
  dialog bare-newline
  [ "${status}" -eq 125 ]
  # shellcheck disable=SC2016 # mini_expect's escapes
  expect='transcript=line1\r\nline2\r\n
result=eof waiting=line1\nline2'
  [ "${output}" = "${expect}" ]
  dialog cr-newline
  [ "${status}" -eq 0 ]
  # shellcheck disable=SC2016 # mini_expect's escapes
  expect='transcript=line1\r\nline2\r\n
result=ok status=0'
  [ "${output}" = "${expect}" ]
}

@test "a prompt that arrives in two reads two seconds apart still matches" {
  dialog split
  [ "${status}" -eq 0 ]
  [ "${lines[2]}" = "result=ok status=0" ]
  case ${lines[0]} in
    'matched=Password:  end=10 reads='*) ;;
    *) false ;;
  esac
  reads=${lines[0]##*reads=}
  [ "${reads}" -ge 2 ]
}

@test "a pattern equal to the text just sent matches the terminal's echo, not the program's answer" {
  dialog echo-match
  [ "${status}" -eq 0 ]
  # transcript: "cmd> " is 5 bytes, the echo "ok" ends at 7, the answer "ran:ok" ends at 15
  case ${lines[1]} in
    'matched=ok end=7 reads='*) ;;
    *) false ;;
  esac
  dialog output-match
  [ "${status}" -eq 0 ]
  if echoes_twice; then
    answer_end=19
  else
    answer_end=15
  fi
  case ${lines[1]} in
    "matched=ran:ok end=${answer_end} reads="*) ;;
    *) false ;;
  esac
}

@test "a pattern that never comes: the wait ends after its limit with 124 and the program is killed" {
  start=${SECONDS}
  dialog timeout
  elapsed=$((SECONDS - start))
  [ "${status}" -eq 124 ]
  # shellcheck disable=SC2016 # mini_expect's escapes
  expect='transcript=started\r\n
result=timeout waiting=never'
  [ "${output}" = "${expect}" ]
  [ "${elapsed}" -ge 2 ]
  pid=$(cat "${WORK}/pid")
  [ -n "${pid}" ]
  if kill -0 "${pid}" 2>/dev/null; then
    false
  fi
}

@test "a program that exits before the pattern: end of file is reported at once, not after the limit" {
  start=${SECONDS}
  dialog eof
  elapsed=$((SECONDS - start))
  [ "${status}" -eq 125 ]
  # shellcheck disable=SC2016 # mini_expect's escapes
  expect='transcript=bye\r\n
result=eof waiting=Password: '
  [ "${output}" = "${expect}" ]
  # the limit in this dialog is 20 seconds
  [ "${elapsed}" -lt 15 ]
}

@test "closing the terminal right after the last send hangs the program up before it finishes its work" {
  dialog close-early
  [ ! -e "${WORK}/marker" ]
  [ "${lines[0]}" = "transcript=go? " ]
  case $(lab_shell_family) in
    # killed by SIGHUP (1): 128 + 1
    bash | dash | posh | mksh | ksh93 | yash | busybox) [ "${lines[1]}" = "result=closed status=129" ] ;;
    # zsh catches the hangup and exits with status 1
    zsh | zsh-sh) [ "${lines[1]}" = "result=closed status=1" ] ;;
    *) false ;;
  esac
}

@test "waiting for end of file instead lets the program finish" {
  dialog wait-eof
  [ "${status}" -eq 0 ]
  [ -e "${WORK}/marker" ]
  [ "${lines[1]}" = "result=ok status=0" ]
  case ${lines[0]} in
    *'done y\r\n') ;;
    *) false ;;
  esac
}

@test "a stdio program on a pty: the prompt arrives, the answer is echoed, lines end in CR NL" {
  dialog stdio-pty
  [ "${status}" -eq 0 ]
  # shellcheck disable=SC2016 # mini_expect's escapes
  expect='transcript=stdio target\r\nready\r\ngo\r\ngot go\r\n
result=ok status=0'
  [ "${output}" = "${expect}" ]
}

@test "the same program on pipes holds its prompt in the stdio buffer: waiting for it times out" {
  dialog stdio-pipe
  [ "${status}" -eq 124 ]
  # glibc and the macOS library hold both lines; musl writes the first line of a stream at once
  case $(libc_flavor) in
    glibc | apple) [ "${lines[0]}" = 'transcript=' ] ;;
    musl) [ "${lines[0]}" = 'transcript=stdio target\n' ] ;;
    *) false ;;
  esac
  [ "${lines[1]}" = "result=timeout waiting=ready" ]
}

@test "on pipes the prompt appears only at exit, after the answer was sent blind, with no echo and no CR" {
  dialog stdio-pipe-blind
  [ "${status}" -eq 0 ]
  # shellcheck disable=SC2016 # mini_expect's escapes
  expect='transcript=stdio target\nready\ngot go\n
result=ok status=0'
  [ "${output}" = "${expect}" ]
}

# interactive NAME: run a dialog whose program is the shell under test started with -i and, per
# family, the option that keeps it from reading startup files.
interactive() {
  case $(lab_shell_family) in
    bash) dialog "$1" --norc ;;
    zsh | zsh-sh) dialog "$1" -f ;;
    yash) dialog "$1" --norcfile ;;
    dash | busybox | posh | mksh | ksh93) dialog "$1" ;;
    *) false ;;
  esac
}

@test "the interactive shell as the program, prompt taken from PS1 in the environment: each family's bytes" {
  interactive shell
  pad=$(printf '%78s' '')
  case $(lab_shell_family) in
    bash)
      # readline echoes the line itself, and an interactive bash prints "exit" when it leaves
      [ "${status}" -eq 7 ]
      # shellcheck disable=SC2016 # mini_expect's escapes and text typed to the driven shell
      [ "${lines[0]}" = 'transcript=LAB> echo $((6*7))\r\n42\r\nLAB> exit 7\r\nexit\r\n' ]
      ;;
    dash | posh)
      # no line editor: the terminal's own echo
      [ "${status}" -eq 7 ]
      # shellcheck disable=SC2016 # mini_expect's escapes and text typed to the driven shell
      [ "${lines[0]}" = 'transcript=LAB> echo $((6*7))\r\n42\r\nLAB> exit 7\r\n' ]
      ;;
    mksh)
      # the line editor ends an accepted line with CR CR NL
      [ "${status}" -eq 7 ]
      # shellcheck disable=SC2016 # mini_expect's escapes and text typed to the driven shell
      [ "${lines[0]}" = 'transcript=LAB> echo $((6*7))\r\r\n42\r\nLAB> exit 7\r\r\n' ]
      ;;
    zsh | zsh-sh)
      # before each prompt: "%", 78 spaces, CR, space, CR, CR (the partial-line marker); after it:
      # the bracketed-paste switch ESC [ ? 2004 h, even with TERM=dumb
      [ "${status}" -eq 7 ]
      # shellcheck disable=SC2016 # mini_expect's escapes and text typed to the driven shell
      expect='transcript=%'"${pad}"'\r \r\rLAB> \x1b[?2004hecho $((6*7))\x1b[?2004l\r\r\n42\r\n%'"${pad}"'\r \r\rLAB> \x1b[?2004hexit 7\x1b[?2004l\r\r\n'
      [ "${lines[0]}" = "${expect}" ]
      ;;
    busybox)
      # after a prompt the line editor asks the terminal for the cursor position (ESC [ 6 n) unless
      # input is already pending, so each query is present or absent depending on timing
      # (Debian's build prints a banner first, Alpine's does not)
      [ "${status}" -eq 7 ]
      # shellcheck disable=SC2016 # mini_expect's escapes and text typed to the driven shell
      case ${lines[0]} in
        *'LAB> \x1b[6necho $((6*7))\r\n42\r\nLAB> \x1b[6nexit 7\r\n') ;;
        *'LAB> \x1b[6necho $((6*7))\r\n42\r\nLAB> exit 7\r\n') ;;
        *'LAB> echo $((6*7))\r\n42\r\nLAB> \x1b[6nexit 7\r\n') ;;
        *'LAB> echo $((6*7))\r\n42\r\nLAB> exit 7\r\n') ;;
        *) false ;;
      esac
      ;;
    ksh93)
      # ksh93 switches the terminal mode around each read, so how often a line is echoed varies
      # from run to run; the result and the absence of escape sequences do not
      [ "${status}" -eq 7 ]
      case ${lines[0]} in
        'transcript=LAB> '*'\r\n42\r\nLAB> exit 7\r\n'*) ;;
        *) false ;;
      esac
      case ${lines[0]} in
        *'\x1b'*) false ;;
        *) ;;
      esac
      ;;
    yash)
      # yash 2.58.1 does not take PS1 from the environment: its prompt stays "$ " and the wait times out
      [ "${status}" -eq 124 ]
      # shellcheck disable=SC2016 # a literal dollar sign
      [ "${lines[0]}" = 'transcript=$ ' ]
      [ "${lines[1]}" = 'result=timeout waiting=LAB> ' ]
      ;;
    *) false ;;
  esac
  if [ "${status}" -eq 7 ]; then
    [ "${lines[${#lines[@]} - 1]}" = "result=ok status=7" ]
  fi
}

@test "setting the prompt in band, with an assignment whose echo cannot match, works in every shell" {
  interactive shell-inband
  [ "${status}" -eq 7 ]
  [ "${lines[${#lines[@]} - 1]}" = "result=ok status=7" ]
  # the type-ahead is echoed by the terminal before the shell starts; LAB''> is not LAB>
  case ${output} in
    "transcript=PS1='LAB''> '\r\n"*'LAB> '*'\r\n42\r\n'*) ;;
    *) false ;;
  esac
}
