#!/usr/bin/env bats
# Tests for terminal multiplexers, shown with minimux.c: a server that holds a pty master and
# outlives its clients, with the shell under test running pane.sh inside the pane.

setup() {
  load ../lib/lab
  SCRIPT="${BATS_TEST_DIRNAME}/mux.sh"
  MUX="${BATS_TEST_TMPDIR}/minimux"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror "${BATS_TEST_DIRNAME}/minimux.c" -o "${MUX}"
}

# Stop every server a test started, by its socket, so nothing outlives the test.
teardown() {
  local sock
  [ -f "${BATS_TEST_TMPDIR}/sockets" ] || return 0
  while IFS= read -r sock; do
    "${MUX}" kill-server "${sock}" >/dev/null 2>&1 || true
  done <"${BATS_TEST_TMPDIR}/sockets"
}

# mux MODE: run mux.sh MODE under the shell under test, which is also the shell inside the pane.
mux() {
  lab_shell_words
  run_lab "${SCRIPT}" "$1" "${MUX}" "${BATS_TEST_TMPDIR}" "${LAB_SHELL_WORDS[@]}"
}

@test "the pane's shell leads its own session on its own terminal, and the server has no terminal at all" {
  mux ids
  [ "${status}" -eq 0 ]
  [ "${lines[0]}" = "a: ready" ]
  [ "${lines[1]}" = "a: stdin_tty=1 shell_is_leader=1 fg_is_shell_group=1 dev_tty=1" ]
  [ "${lines[2]}" = "a: [end: closed by the client]" ]
  [ "${lines[3]}" = "server_tty=0 server_leader=1" ]
  [ "${#lines[@]}" -eq 4 ]
}

@test "a client detaches: the pane is the same running process, and the server kept what it wrote meanwhile" {
  mux detach
  [ "${status}" -eq 0 ]
  expect="a: ready
a: got:hello
a: [end: closed by the client]
b: ready
b: got:hello
b: printed-while-detached clients=0
b: got:still here
b: [end: closed by the client]
pane: same process, still running
clients=0 detached_bytes=41"
  [ "${output}" = "${expect}" ]
}

@test "detaching sends the pane no SIGHUP; killing the server closes the master and the kernel sends one" {
  mux hangup
  [ "${status}" -eq 0 ]
  [ "${lines[0]}" = "after the client detached: pane=running sighup=0" ]
  [ "${lines[1]}" = "after the server was killed: sighup=1" ]
  [ "${#lines[@]}" -eq 2 ]
}

@test "one pty has one size: under the smallest policy it is the minimum of the attached clients in each dimension" {
  mux smallest
  [ "${status}" -eq 0 ]
  expect="clients=0 rows=24 cols=80
a: ready
a: size:40 100
a: size:24 80
a: size:40 30
a: size:40 100
a: [end: closed by the client]
clients=0 rows=40 cols=100"
  [ "${output}" = "${expect}" ]
}

@test "under the latest policy the size follows whichever client typed last" {
  mux latest
  [ "${status}" -eq 0 ]
  expect="a: ready
a: size:40 100
a: size:24 80
a: size:40 100
a: [end: closed by the client]"
  [ "${output}" = "${expect}" ]
}

@test "a resize reaches the pane as one SIGWINCH; attaching at the size it already has sends none" {
  mux winch
  [ "${status}" -eq 0 ]
  [ "${lines[0]}" = "attached at the size the pane already had: sigwinch=0" ]
  [ "${lines[1]}" = "attached at 30x90: sigwinch=1" ]
  [ "${#lines[@]}" -eq 2 ]
}

@test "after a resize stty size is right in every shell, but a script's own COLUMNS and LINES depend on the shell" {
  mux cols
  [ "${status}" -eq 0 ]
  [ "${lines[0]}" = "a: ready" ]
  [ "${lines[2]}" = "a: size:30 90" ]
  [ "${lines[6]}" = "a: [end: closed by the client]" ]
  [ "${#lines[@]}" -eq 7 ]
  unset_both="a: cols:unset lines:unset"
  # at 24x80, after the resize to 30x90, the checkwinsize answer, and after that option
  observed="${lines[1]} | ${lines[3]} | ${lines[4]} | ${lines[5]}"
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        3.* | 4.[0-2].*) # the option exists, but before bash 4.3 a script never acts on it
          [ "${observed}" = "${unset_both} | ${unset_both} | a: checkwinsize:on | ${unset_both}" ]
          ;;
        4.*) # 4.3 and 4.4: off by default; once on, the size is read after each external command
          [ "${observed}" = "${unset_both} | ${unset_both} | a: checkwinsize:on | a: cols:90 lines:30" ]
          ;;
        5.*) # on by default since bash 5.0
          [ "${observed}" = "a: cols:80 lines:24 | a: cols:90 lines:30 | a: checkwinsize:on | a: cols:90 lines:30" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | mksh | ksh93) # these shells keep both variables current on their own
      [ "${observed}" = "a: cols:80 lines:24 | a: cols:90 lines:30 | a: checkwinsize:unavailable | a: cols:90 lines:30" ]
      ;;
    dash | posh | busybox | yash) # these shells never set either variable in a script
      [ "${observed}" = "${unset_both} | ${unset_both} | a: checkwinsize:unavailable | ${unset_both}" ]
      ;;
    *) false ;;
  esac
}

@test "two attached clients share one pane: either one can type, and both receive every line" {
  mux broadcast
  [ "${status}" -eq 0 ]
  expect="a: ready
a: got:from a
a: got:from b
a: [end: pane exited with status 0]
b: ready
b: got:from a
b: got:from b
b: [end: pane exited with status 0]"
  [ "${output}" = "${expect}" ]
}

@test "the server consumes the prefix key: prefix d detaches, prefix twice sends one byte, an unbound key is dropped" {
  mux prefix
  [ "${status}" -eq 0 ]
  expect="a: ready
a: hex:610262
a: got:q
a: [end: detached by the server]
b: ready
b: hex:610262
b: got:q
b: got:xy
b: [end: closed by the client]"
  [ "${output}" = "${expect}" ]
}

@test "a pane keeps the environment its server had: a client started later with another SSH_AUTH_SOCK changes nothing" {
  mux env
  [ "${status}" -eq 0 ]
  [ "${lines[0]}" = "client: SSH_AUTH_SOCK=/new/agent" ]
  [ "${lines[1]}" = "a: ready" ]
  [ "${lines[2]}" = "a: env:/old/agent" ]
  [ "${lines[3]}" = "a: [end: closed by the client]" ]
  [ "${#lines[@]}" -eq 4 ]
}

@test "a server killed with SIGKILL leaves a dead socket file: ECONNREFUSED until a new server or kill-server replaces it" {
  mux stale
  [ "${status}" -eq 0 ]
  expect="running: clients=0
server killed with SIGKILL: connect=ECONNREFUSED socket-file=present
new server on the stale socket: clients=0
second server on a live socket: bind=EADDRINUSE
after kill-server: connect=ENOENT socket-file=absent"
  [ "${output}" = "${expect}" ]
}

@test "the socket is created owner-only whatever the umask, and without write permission connect fails with EACCES" {
  mux perms
  [ "${status}" -eq 0 ]
  [ "${lines[0]}" = "created under umask 000: srw-------" ]
  uid=$(id -u)
  if [ "${uid}" -eq 0 ]; then
    [ "${lines[1]}" = "mode 000: clients=0" ] # the superuser passes every permission check
  else
    [ "${lines[1]}" = "mode 000: connect=EACCES" ]
  fi
  [ "${lines[2]}" = "mode 600: clients=0" ]
  [ "${#lines[@]}" -eq 3 ]
}

@test "sun_path is 104 bytes on macOS and 108 on Linux; a longer name fails, a deep directory does not" {
  mux sunpath
  [ "${status}" -eq 0 ]
  case $(uname -s) in
    Darwin) [ "${lines[0]}" = "sun_path=104" ] ;;
    Linux) [ "${lines[0]}" = "sun_path=108" ] ;;
    *) false ;;
  esac
  [ "${lines[1]}" = "name one byte shorter than sun_path: clients=0" ]
  [ "${lines[2]}" = "bind=ENAMETOOLONG" ]
  [ "${lines[3]}" = "status=1" ]
  [ "${lines[4]}" = "short name in a deep directory: path longer than sun_path=yes clients=0" ]
  [ "${#lines[@]}" -eq 5 ]
}

@test "when the pane exits, attached clients get its status, the socket is removed, and the server is gone" {
  mux exit
  [ "${status}" -eq 0 ]
  [ "${lines[0]}" = "a: ready" ]
  [ "${lines[1]}" = "a: [end: pane exited with status 7]" ]
  [ "${lines[2]}" = "afterwards: connect=ENOENT socket-file=absent" ]
  [ "${#lines[@]}" -eq 3 ]
}

@test "mux.sh started by its bare name from the lab directory still finds pane.sh" {
  cd "${BATS_TEST_DIRNAME}"
  lab_shell_words
  run_lab mux.sh exit "${MUX}" "${BATS_TEST_TMPDIR}" "${LAB_SHELL_WORDS[@]}"
  [ "${status}" -eq 0 ]
  [ "${lines[0]}" = "a: ready" ]
  [ "${lines[1]}" = "a: [end: pane exited with status 7]" ]
  [ "${lines[2]}" = "afterwards: connect=ENOENT socket-file=absent" ]
  [ "${#lines[@]}" -eq 3 ]
}
