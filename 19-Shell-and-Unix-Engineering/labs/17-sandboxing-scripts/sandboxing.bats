#!/usr/bin/env bats
# Tests for the sandboxing-scripts lab: the cooperative layer (confine.sh) and the kernel layer
# (kconfine.c), and the boundary between them.

setup() {
  load ../lib/lab
  CONFINE="$BATS_TEST_DIRNAME/confine.sh"
  KCONFINE="$BATS_TEST_TMPDIR/kconfine"
  SCRATCH="$BATS_TEST_TMPDIR/scratch"
  mkdir -p "$SCRATCH"
  PHYS=$(cd "$SCRATCH" && pwd -P)
}

# build_kconfine: compile the kernel-layer probe; a missing compiler fails the test.
build_kconfine() {
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -Wno-deprecated-declarations \
    -o "$KCONFINE" "$BATS_TEST_DIRNAME/kconfine.c"
}

# limit_mode: whether `confine.sh -f` can enforce a limit in the shell under test.
# posh has no ulimit builtin (see Resource-Limits-and-ulimit), so confine.sh must refuse.
limit_mode() {
  case $(lab_shell_family) in
    posh) echo refused ;;
    bash | zsh | zsh-sh | yash | busybox | mksh | ksh93 | dash) echo enforced ;;
    *) echo "unexpected shell family" >&2 ;;
  esac
}

@test "confine.sh passes exactly the allow-listed environment and drops hostile variables" {
  export DEMO_SECRET=EXAMPLE-NOT-A-REAL-KEY BASH_ENV=/nonexistent ENV=/nonexistent
  export LD_LIBRARY_PATH=/nonexistent
  # Control: without confine.sh the same variables reach the child.
  run /usr/bin/env
  [[ $output == *"DEMO_SECRET=EXAMPLE-NOT-A-REAL-KEY"* ]]
  [[ $output == *"BASH_ENV=/nonexistent"* ]]
  run_lab "$CONFINE" "$SCRATCH" /usr/bin/env
  [ "$status" -eq 0 ]
  sorted=$(printf '%s\n' "$output" | LC_ALL=C sort)
  expected=$(printf '%s\n' "HOME=$PHYS" "LANG=C" "LC_ALL=C" "PATH=/usr/bin:/bin" "TMPDIR=$PHYS")
  [ "$sorted" = "$expected" ]
}

@test "confine.sh gives the command /dev/null as standard input" {
  printf 'secret-input\n' >"$BATS_TEST_TMPDIR/in"
  # Control: an unconfined cat reads the file on its stdin.
  run_lab_c 'exec /bin/cat' lab <"$BATS_TEST_TMPDIR/in"
  [ "$output" = secret-input ]
  run_lab "$CONFINE" "$SCRATCH" /bin/cat <"$BATS_TEST_TMPDIR/in"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "confine.sh closes descriptors 3-9 that the parent leaked" {
  printf 'leaked-secret\n' >"$BATS_TEST_TMPDIR/secret"
  exec 7<"$BATS_TEST_TMPDIR/secret"
  # Control: without confine.sh the child reads the secret through the inherited descriptor.
  run_lab_c '/bin/cat <&7'
  [ "$status" -eq 0 ]
  [ "$output" = leaked-secret ]
  run_lab "$CONFINE" "$SCRATCH" /bin/sh -c '/bin/cat <&7'
  [ "$status" -ne 0 ]
  [[ $output != *leaked-secret* ]]
  exec 7<&-
}

@test "confine.sh runs in SCRATCH with umask 077 even when the parent umask is 022" {
  umask 022
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c ': > "$1/control"; /bin/ls -l "$1/control"' lab "$SCRATCH"
  case $output in
    -rw-r--r--*) ;;
    *) false ;;
  esac
  run_lab "$CONFINE" "$SCRATCH" /bin/sh -c 'pwd -P; : > made; /bin/ls -l made'
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "$PHYS" ]
  case ${lines[1]} in
    -rw-------*) ;;
    *) false ;;
  esac
}

@test "confine.sh -f 0 sets the file-size and core limits the command inherits" {
  run_lab "$CONFINE" -f 0 "$SCRATCH" /bin/sh -c 'ulimit -f; ulimit -c'
  case $(limit_mode) in
    enforced)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = 0 ]
      [ "${lines[1]}" = 0 ]
      ;;
    refused) [ "$status" -eq 125 ] ;;
    *) false ;;
  esac
}

@test "a write past the file-size limit kills the command with SIGXFSZ" {
  # Control: without -f the same dd writes its byte.
  run_lab "$CONFINE" "$SCRATCH" /bin/dd if=/dev/zero of=ok bs=1 count=1
  [ "$status" -eq 0 ]
  size=$(wc -c <"$SCRATCH/ok")
  [ "$size" -eq 1 ]
  run_lab "$CONFINE" -f 0 "$SCRATCH" /bin/dd if=/dev/zero of=big bs=1 count=1
  case $(limit_mode) in
    enforced)
      [ "$status" -eq 153 ] # 128 + SIGXFSZ (25) on Linux and macOS
      [ -e "$SCRATCH/big" ]
      [ ! -s "$SCRATCH/big" ]
      ;;
    refused)
      [ "$status" -eq 125 ]
      [ ! -e "$SCRATCH/big" ]
      ;;
    *) false ;;
  esac
}

@test "confine.sh fails closed on a relative or name=value command, a bad scratch, or a bare -f" {
  run_lab "$CONFINE" "$SCRATCH" echo hi
  [ "$status" -eq 125 ]
  run_lab "$CONFINE" "$SCRATCH" FOO=bar /usr/bin/env
  [ "$status" -eq 125 ]
  run_lab "$CONFINE" "$BATS_TEST_TMPDIR/no-such-dir" /usr/bin/env
  [ "$status" -eq 125 ]
  run_lab "$CONFINE" -f
  [ "$status" -eq 125 ]
  run_lab "$CONFINE"
  [ "$status" -eq 125 ]
}

@test "confine.sh is not filesystem confinement: the command still writes outside SCRATCH" {
  mkdir "$BATS_TEST_TMPDIR/outside"
  # shellcheck disable=SC2016 # the expansion belongs to the payload shell
  run_lab "$CONFINE" "$SCRATCH" /bin/sh -c 'echo escaped > "$1"' sh "$BATS_TEST_TMPDIR/outside/marker"
  [ "$status" -eq 0 ]
  marker=$(cat "$BATS_TEST_TMPDIR/outside/marker")
  [ "$marker" = escaped ]
}

@test "confine.sh does not stop network access: connect(2) is attempted and fails for other reasons" {
  build_kconfine
  run_lab "$CONFINE" "$SCRATCH" "$KCONFINE" connect
  [ "$status" -eq 0 ]
  [[ $output == connect:* ]]
  [ "$output" != "connect: EPERM" ]
}

@test "kconfine: connect(2) is denied after confinement, in a program exec'd afterwards" {
  build_kconfine
  run_lab "$CONFINE" "$SCRATCH" "$KCONFINE" confine "$KCONFINE" connect
  [ "$status" -eq 0 ]
  [ "$output" = "connect: EPERM" ]
}

@test "kconfine is a deny-list: unrelated work, including writes and forks, still succeeds" {
  build_kconfine
  run_lab "$CONFINE" "$SCRATCH" "$KCONFINE" confine /bin/sh -c ': > made && /bin/echo wrote'
  [ "$status" -eq 0 ]
  [ "$output" = wrote ]
  [ -e "$SCRATCH/made" ]
}

@test "the kernel layer is visible to the platform: /proc on Linux, Seatbelt file-write deny on macOS" {
  build_kconfine
  case $(uname -s) in
    Linux)
      run_lab "$CONFINE" "$SCRATCH" "$KCONFINE" confine /bin/grep -E '^(NoNewPrivs|Seccomp):' /proc/self/status
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "$(printf 'NoNewPrivs:\t1')" ]
      [ "${lines[1]}" = "$(printf 'Seccomp:\t2')" ]
      ;;
    Darwin)
      run_lab "$CONFINE" "$SCRATCH" /bin/sh -c ': > allowed'
      [ "$status" -eq 0 ]
      [ -e "$SCRATCH/allowed" ]
      run_lab "$CONFINE" "$SCRATCH" /usr/bin/sandbox-exec -p '(version 1)(allow default)(deny file-write*)' /bin/sh -c ': > blocked'
      [ "$status" -ne 0 ]
      [ ! -e "$SCRATCH/blocked" ]
      ;;
    *) false ;;
  esac
}
