#!/usr/bin/env bats
# Tests for the Coprocesses note: which shells have a coprocess, how bash, ksh and zsh wire one,
# where the descriptors go, how end of file and exit are signalled, and why stdio buffering
# deadlocks a request-response loop in every shell.
# shellcheck disable=SC2030,SC2031 # bats run sets output and status in the test shell, not a subshell

setup_file() {
  export REPLY_BIN="$BATS_FILE_TMPDIR/reply"
  export FDCHECK_BIN="$BATS_FILE_TMPDIR/fdcheck"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -o "$REPLY_BIN" "$BATS_TEST_DIRNAME/reply.c"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -o "$FDCHECK_BIN" "$BATS_TEST_DIRNAME/fdcheck.c"
}

setup() {
  load ../lib/lab
  DIALECTS="$BATS_TEST_DIRNAME/dialects.sh"
  BASHP="$BATS_TEST_DIRNAME/coproc-bash.sh"
  KSHP="$BATS_TEST_DIRNAME/coproc-ksh.sh"
  ZSHP="$BATS_TEST_DIRNAME/coproc-zsh.sh"
  FIFO="$BATS_TEST_DIRNAME/fifo-pair.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
  # A probe that misparses >&p in a shell without coprocesses writes a file named p; keep it here.
  cd "$BATS_TEST_TMPDIR" || return 1
}

# coproc_kind: bash for bash 4.0 and later, bash32 for bash 3.x, otherwise the shell family.
coproc_kind() {
  local family
  family=$(lab_shell_family)
  if [ "$family" = bash ]; then
    case $(lab_shell_version) in
      3.*) echo bash32 ;;
      *) echo bash ;;
    esac
  else
    printf '%s\n' "$family"
  fi
}

# assert_bash_syntax_rejected: the bash-only probe did not run; bash 3.2 reports a syntax error.
assert_bash_syntax_rejected() {
  local kind
  [ "$status" -ne 0 ]
  kind=$(coproc_kind)
  if [ "$kind" = bash32 ]; then
    [ "$status" -eq 2 ]
    [[ $output == *"syntax error"* ]]
  fi
}

# --- which shells have one ----------------------------------------------------------------------

@test "each shell accepts only its own coprocess syntax, and POSIX-only shells accept none" {
  run_lab "$DIALECTS"
  [ "$status" -eq 0 ]
  case $(coproc_kind) in
    bash) [ "$output" = "bash=yes ksh=no zsh=no" ] ;;
    zsh | zsh-sh) [ "$output" = "bash=no ksh=no zsh=yes" ] ;;
    ksh93 | mksh) [ "$output" = "bash=no ksh=yes zsh=no" ] ;;
    bash32 | dash | posh | busybox | yash) [ "$output" = "bash=no ksh=no zsh=no" ] ;;
    *) false ;;
  esac
}

# --- bash coproc --------------------------------------------------------------------------------

@test "bash puts NAME[0] and NAME[1] on the highest free descriptors below 64, and only 5.3 allows two coprocs silently" {
  run_lab "$BASHP" fds
  case $(coproc_kind) in
    bash)
      [ "$status" -eq 0 ]
      # sh_openpipe moves each pipe end to the highest free descriptor below 64:
      # C gets 63 (read) and 60 (write); D gets 62 and 58 because 61 and 59 are the child ends.
      [ "${lines[${#lines[@]} - 1]}" = "C=63 60 D=62 58 C_PID_live=yes" ]
      case $(lab_shell_version) in
        4.* | 5.[0-2]*)
          [ "${#lines[@]}" -eq 2 ]
          [[ ${lines[0]} == *"execute_coproc: coproc ["*":C] still exists"* ]]
          ;;
        5.[3-9]* | [6-9].*) [ "${#lines[@]}" -eq 1 ] ;;
        *) false ;;
      esac
      ;;
    bash32 | zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_bash_syntax_rejected ;;
    *) false ;;
  esac
}

@test "bash: wc -l answers only after the write end is closed, and closing it sets NAME[1] to -1" {
  run_lab "$BASHP" eof
  case $(coproc_kind) in
    bash)
      [ "$status" -eq 0 ]
      [ "$output" = "count=3 write_fd_after_close=-1" ]
      ;;
    bash32 | zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_bash_syntax_rejected ;;
    *) false ;;
  esac
}

@test "bash: once the coprocess is reaped NAME is unset and its unread output is lost unless the descriptor was copied" {
  run_lab "$BASHP" lost
  case $(coproc_kind) in
    bash)
      [ "$status" -eq 0 ]
      [ "$output" = "vars=unset direct=failed saved=hi" ]
      ;;
    bash32 | zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_bash_syntax_rejected ;;
    *) false ;;
  esac
}

@test "bash: the descriptors are close-on-exec, closed in subshells, open in command substitutions, and passed by redirection" {
  run_lab "$BASHP" inherit "$FDCHECK_BIN"
  case $(coproc_kind) in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "external: 63:closed 60:closed" ]
      [ "${lines[1]}" = "subshell=closed comsub=open redirected: 1:open" ]
      [ "${#lines[@]}" -eq 2 ]
      ;;
    bash32 | zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_bash_syntax_rejected ;;
    *) false ;;
  esac
}

@test "bash: writing to a coprocess that has exited kills the script with SIGPIPE (status 141)" {
  run_lab "$BASHP" sigpipe
  case $(coproc_kind) in
    bash)
      [ "$status" -eq 141 ]
      [ "$output" = "" ]
      ;;
    bash32 | zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_bash_syntax_rejected ;;
    *) false ;;
  esac
}

@test "bash: with SIGPIPE ignored the same write fails with EPIPE and the script goes on" {
  run_lab "$BASHP" sigpipe-ignored
  case $(coproc_kind) in
    bash)
      [ "$status" -eq 0 ]
      [[ ${lines[0]} == *"write error: Broken pipe"* ]]
      [ "${lines[1]}" = "write_status=1" ]
      ;;
    bash32 | zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_bash_syntax_rejected ;;
    *) false ;;
  esac
}

@test "bash: the coproc command returns 0 at once, and wait on NAME_PID returns the command's status" {
  run_lab "$BASHP" status
  case $(coproc_kind) in
    bash)
      [ "$status" -eq 0 ]
      [ "$output" = "coproc_status=0 wait_status=7" ]
      ;;
    bash32 | zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_bash_syntax_rejected ;;
    *) false ;;
  esac
}

# --- Korn shell |& ------------------------------------------------------------------------------

@test "ksh: cmd |& is reached with print -p and read -p, and exec 3>&p; exec 3>&- sends end of file" {
  run_lab "$KSHP" round </dev/null 3>&-
  local round_status=$status round_output=$output
  run_lab "$KSHP" eof </dev/null 3>&-
  case $(coproc_kind) in
    ksh93 | mksh)
      [ "$round_status" -eq 0 ]
      [ "$round_output" = "round=hi" ]
      [ "$status" -eq 0 ]
      [ "$output" = "count=3" ]
      ;;
    bash | bash32 | zsh | zsh-sh | dash | posh | busybox | yash)
      [[ $round_output != *"round=hi"* ]]
      [[ $output != *"count=3"* ]]
      ;;
    *) false ;;
  esac
}

@test "ksh: a second |& while the first co-process runs is an error that ends the script" {
  run_lab "$KSHP" second </dev/null 3>&-
  case $(coproc_kind) in
    ksh93 | mksh)
      [ "$status" -eq 1 ]
      [[ $output != *started* ]]
      local kind version
      kind=$(coproc_kind)
      version=$(lab_shell_version)
      case $kind:$version in
        ksh93:*93u+m*) [[ $output == *"coprocess is running; cannot create a new coprocess"* ]] ;;
        ksh93:*) [[ $output == *"process already exists"* ]] ;;
        mksh:*) [[ $output == *"coprocess already exists"* ]] ;;
        *) false ;;
      esac
      ;;
    bash | bash32 | zsh | zsh-sh | dash | posh | busybox | yash)
      [[ $output != *"already exists"* ]]
      [[ $output != *"cannot create a new coprocess"* ]]
      ;;
    *) false ;;
  esac
}

@test "ksh: exec 3>&p 4<&p moves the co-process to plain descriptors and frees |& for another" {
  run_lab "$KSHP" move </dev/null 3>&-
  case $(coproc_kind) in
    ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = "new=two old=three" ]
      ;;
    bash | bash32 | zsh | zsh-sh | dash | posh | busybox | yash) [[ $output != *"new=two old=three"* ]] ;;
    *) false ;;
  esac
}

# --- zsh coproc ---------------------------------------------------------------------------------

@test "zsh: coproc cmd is reached with print -p, read -p and >&p, also under emulate sh" {
  run_lab "$ZSHP" round </dev/null 3>&-
  case $(coproc_kind) in
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "round=hi external=external" ]
      ;;
    bash | bash32 | ksh93 | mksh | dash | posh | busybox | yash) [[ $output != *"round=hi external=external"* ]] ;;
    *) false ;;
  esac
}

@test "zsh: a new coproc replaces the old one, which survives only through descriptors copied first" {
  run_lab "$ZSHP" replace </dev/null 3>&-
  case $(coproc_kind) in
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "new=two old=three" ]
      ;;
    bash | bash32 | ksh93 | mksh | dash | posh | busybox | yash) [[ $output != *"new=two old=three"* ]] ;;
    *) false ;;
  esac
}

@test "zsh: coproc exit closes the write end, so wc -l sees end of file and answers on the saved descriptor" {
  run_lab "$ZSHP" eof </dev/null 3>&-
  case $(coproc_kind) in
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "count=3" ]
      ;;
    bash | bash32 | ksh93 | mksh | dash | posh | busybox | yash) [[ $output != *"count=3"* ]] ;;
    *) false ;;
  esac
}

# --- the portable version, and the buffering deadlock -------------------------------------------

@test "two FIFOs make a coprocess in every shell: a line-buffered filter answers each request at once" {
  run_lab "$FIFO" "$REPLY_BIN" line a b c
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "a -> ack a" ]
  [ "${lines[1]}" = "b -> ack b" ]
  [ "${lines[2]}" = "c -> ack c" ]
  [ "${lines[3]}" = "after_eof=" ]
  [ "${lines[4]}" = "status=0" ]
  [ "${#lines[@]}" -eq 5 ]
}

@test "a fully buffered filter holds its answer until end of file, so a request-response loop stalls" {
  run_lab "$FIFO" "$REPLY_BIN" full q
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "q -> timeout" ]
  [ "${lines[1]}" = "after_eof=ack q" ]
  [ "${lines[2]}" = "status=0" ]
  [ "${#lines[@]}" -eq 3 ]
}
