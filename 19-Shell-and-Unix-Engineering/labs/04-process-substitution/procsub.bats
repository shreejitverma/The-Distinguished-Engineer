#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031 # bats run sets output and status in the test shell, not a subshell
# Process substitution per shell family: what <(...) and >(...) expand to, what a consumer
# can do with the pathname, and who waits for the substituted process.

setup_file() {
  "${CC:-cc}" -Wall -Wextra -Werror -o "$BATS_FILE_TMPDIR/fdprobe" "$BATS_TEST_DIRNAME/fdprobe.c"
}

setup() {
  load ../lib/lab
  LAB=$BATS_TEST_DIRNAME
  PROBE=$BATS_FILE_TMPDIR/fdprobe
  # ksh93u+ 2012 can create a stray file in the working directory for "> >(...)".
  cd "$BATS_TEST_TMPDIR" || return 1
}

# bash_at_least MAJOR MINOR: the bash under test is at least MAJOR.MINOR.
bash_at_least() {
  local v major rest minor
  v=$(lab_shell_version)
  major=${v%%.*}
  rest=${v#*.}
  minor=${rest%%[!0-9]*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# bash_procsub_on: POSIX mode switches process substitution off before bash 5.1.
bash_procsub_on() {
  local mode
  lab_shell_words
  # shellcheck disable=SC2016 # $SHELLOPTS belongs to the shell under test
  mode=$("${LAB_SHELL_WORDS[@]}" -c 'case :$SHELLOPTS: in *:posix:*) echo posix ;; *) echo normal ;; esac')
  [ "$mode" = normal ] || bash_at_least 5 1
}

# support: path (a pathname word), redirection (yash), or none (a syntax error).
support() {
  local family
  family=$(lab_shell_family)
  case $family in
    bash)
      if bash_procsub_on; then echo path; else echo none; fi
      ;;
    zsh | zsh-sh | ksh93 | busybox) echo path ;;
    yash) echo redirection ;;
    dash | posh | mksh) echo none ;;
    *)
      echo "unexpected shell family: $family" >&2
      return 1
      ;;
  esac
}

# old_ksh: the shell is ksh93u+ 2012 (macOS /bin/ksh), not ksh93u+m.
old_ksh() {
  case $(lab_shell_version) in
    *93u+m*) return 1 ;;
    *) return 0 ;;
  esac
}

# fd_prefix: the directory zsh's configure chose (/proc/self/fd first, then /dev/fd).
fd_prefix() {
  case $(uname -s) in
    Linux) echo /proc/self/fd ;;
    Darwin) echo /dev/fd ;;
    *) return 1 ;;
  esac
}

# assert_syntax_error: the construct was rejected by the parser.
assert_syntax_error() {
  [ "$status" -ne 0 ]
  [[ $output == *[Ss]yntax* ]]
}

# assert_eventually FILE TEXT: FILE holds TEXT within three seconds (the consumer did run).
assert_eventually() {
  local got
  lab_poll 3 test -s "$1"
  got=$(cat "$1")
  [ "$got" = "$2" ]
}

# sorted_lines: the captured output lines in C-locale order.
sorted_lines() {
  printf '%s\n' "${lines[@]}" | LC_ALL=C sort
}

# assert_old_ksh_redirect_bug: ksh93u+ 2012 cannot open "> >(...)" as a redirection target.
assert_old_ksh_redirect_bug() {
  [[ $output == *"cannot create"* ]]
}

@test "kind: a pathname word, yash process redirection, or a syntax error" {
  local want
  want=$(support)
  run_lab "$LAB/procsub.sh" kind
  [ "$status" -eq 0 ]
  [ "$output" = "$want" ]
}

@test "paths: each substitution names its own descriptor" {
  local prefix
  run_lab "$LAB/procsub.sh" paths
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = "$(printf '/dev/fd/63\n/dev/fd/62')" ]
      else
        assert_syntax_error
      fi
      ;;
    busybox)
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf '/dev/fd/64\n/dev/fd/65')" ]
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf '/dev/fd/3\n/dev/fd/4')" ]
      ;;
    zsh | zsh-sh)
      prefix=$(fd_prefix)
      [ "$status" -eq 0 ]
      [ "${#lines[@]}" -eq 2 ]
      [ "${lines[0]}" != "${lines[1]}" ]
      [[ ${lines[0]} == "$prefix"/[0-9]* ]]
      [[ ${lines[1]} == "$prefix"/[0-9]* ]]
      [ "${lines[0]##*/}" -ge 10 ]
      [ "${lines[1]##*/}" -ge 10 ]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [ "$output" = "" ]
      ;;
    dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "read: an external consumer reads each producer through its pathname" {
  local want
  want=$(support)
  run_lab "$LAB/procsub.sh" read
  case $want in
    path)
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'a\nb')" ]
      ;;
    redirection)
      [ "$status" -eq 0 ]
      [ "$output" = b ]
      ;;
    none) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "filetype: the pathname opens a pipe, which cannot seek" {
  local want
  want=$(support)
  run_lab "$LAB/procsub.sh" filetype "$PROBE"
  case $want in
    path)
      [ "$status" -eq 0 ]
      [ "$output" = "type=fifo seek=ESPIPE data=hi" ]
      ;;
    redirection)
      # No pathname word: fdprobe gets one argument and prints its usage.
      [ "$status" -eq 2 ]
      [[ $output == usage:* ]]
      ;;
    none) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "closed: a consumer that closes its descriptors cannot open the pathname" {
  local want
  want=$(support)
  run_lab "$LAB/procsub.sh" closed "$PROBE"
  case $want in
    path)
      [ "$status" -eq 0 ]
      case $(uname -s) in
        Linux) [ "$output" = open=ENOENT ] ;;
        Darwin) [ "$output" = open=EBADF ] ;;
        *) false ;;
      esac
      ;;
    redirection)
      [ "$status" -eq 2 ]
      [[ $output == usage:* ]]
      ;;
    none) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "tempfile: zsh =(...) is a seekable regular file that survives closed descriptors" {
  run_lab "$LAB/procsub.sh" tempfile "$PROBE"
  case $(lab_shell_family) in
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'type=regular seek=3 data=hi\nopen=ok\nafter=removed')" ]
      ;;
    bash | busybox | ksh93 | yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "status: the consumer's status hides the producer's; only bash 4.4+ can wait for it" {
  run_lab "$LAB/procsub.sh" status
  case $(lab_shell_family) in
    bash)
      if ! bash_procsub_on; then
        assert_syntax_error
      elif bash_at_least 4 4; then
        [ "$status" -eq 0 ]
        [ "$output" = "$(printf 'status=0\nbang=pid\nwait=7')" ]
      else
        [ "$status" -eq 0 ]
        [ "$output" = "$(printf 'status=0\nbang=pid\nwait=127')" ]
      fi
      ;;
    zsh | zsh-sh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'status=0\nbang=zero\nwait=127')" ]
      ;;
    ksh93 | busybox)
      # $! stays unset, so "wait $!" is a bare wait, which returns 0.
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'status=0\nbang=unset\nwait=0')" ]
      ;;
    dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "async: nothing waits for >(...) on an external command" {
  local f=$BATS_TEST_TMPDIR/out
  run_lab "$LAB/procsub.sh" async "$f"
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = absent ]
        assert_eventually "$f" x
      else
        assert_syntax_error
      fi
      ;;
    zsh | zsh-sh | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = absent ]
      assert_eventually "$f" x
      ;;
    ksh93)
      if old_ksh; then
        assert_old_ksh_redirect_bug
      else
        [ "$status" -eq 0 ]
        [ "$output" = absent ]
        assert_eventually "$f" x
      fi
      ;;
    yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "async-group: only zsh waits for >(...) on a compound command" {
  local got f=$BATS_TEST_TMPDIR/out
  run_lab "$LAB/procsub.sh" async-group "$f"
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = absent ]
        assert_eventually "$f" x
      else
        assert_syntax_error
      fi
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = present ]
      got=$(cat "$f")
      [ "$got" = x ]
      ;;
    busybox)
      [ "$status" -eq 0 ]
      [ "$output" = absent ]
      assert_eventually "$f" x
      ;;
    ksh93)
      if old_ksh; then
        assert_old_ksh_redirect_bug
      else
        [ "$status" -eq 0 ]
        [ "$output" = absent ]
        assert_eventually "$f" x
      fi
      ;;
    yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "async-wait: wait \$! reaches the substitution in bash 4.4+, zsh, and ksh93u+m" {
  local f=$BATS_TEST_TMPDIR/out
  run_lab "$LAB/procsub.sh" async-wait "$f"
  case $(lab_shell_family) in
    bash)
      if ! bash_procsub_on; then
        assert_syntax_error
      elif bash_at_least 4 4; then
        [ "$status" -eq 0 ]
        [ "$output" = present ]
      else
        [ "$status" -eq 0 ]
        [ "$output" = absent ]
        assert_eventually "$f" x
      fi
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = present ]
      ;;
    busybox)
      [ "$status" -eq 0 ]
      [ "$output" = absent ]
      assert_eventually "$f" x
      ;;
    ksh93)
      if old_ksh; then
        assert_old_ksh_redirect_bug
      else
        [ "$status" -eq 0 ]
        [ "$output" = present ]
      fi
      ;;
    yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "async-waitall: a bare wait reaches the substitution in bash 5.1+, zsh, and ksh93u+m" {
  local f=$BATS_TEST_TMPDIR/out
  run_lab "$LAB/procsub.sh" async-waitall "$f"
  case $(lab_shell_family) in
    bash)
      if ! bash_procsub_on; then
        assert_syntax_error
      elif bash_at_least 5 1; then
        [ "$status" -eq 0 ]
        [ "$output" = present ]
      else
        [ "$status" -eq 0 ]
        [ "$output" = absent ]
        assert_eventually "$f" x
      fi
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = present ]
      ;;
    busybox)
      [ "$status" -eq 0 ]
      [ "$output" = absent ]
      assert_eventually "$f" x
      ;;
    ksh93)
      if old_ksh; then
        assert_old_ksh_redirect_bug
      else
        [ "$status" -eq 0 ]
        [ "$output" = present ]
      fi
      ;;
    yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "async-wait-ext: on an external command only ksh93u+m can wait for \$!" {
  local f=$BATS_TEST_TMPDIR/out
  run_lab "$LAB/procsub.sh" async-wait-ext "$f"
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = absent ]
        assert_eventually "$f" x
      else
        assert_syntax_error
      fi
      ;;
    zsh | zsh-sh | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = absent ]
      assert_eventually "$f" x
      ;;
    ksh93)
      if old_ksh; then
        assert_old_ksh_redirect_bug
      else
        [ "$status" -eq 0 ]
        [ "$output" = present ]
      fi
      ;;
    yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "capture: \$(...) waits for a consumer that holds the capture pipe, except in ksh93" {
  run_lab "$LAB/procsub.sh" capture
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = got=hi ]
      else
        # The parse error is inside $(...), so only the substitution fails.
        [ "$status" -eq 0 ]
        [[ $output == *[Ss]yntax* ]]
        [ "${lines[${#lines[@]} - 1]}" = got= ]
      fi
      ;;
    zsh | zsh-sh | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = got=hi ]
      ;;
    ksh93)
      if old_ksh; then
        assert_old_ksh_redirect_bug
      else
        [ "$status" -eq 0 ]
        [ "$output" = got= ]
      fi
      ;;
    yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "capture-released: \$(...) stops waiting once the consumer moves its stdout away" {
  local f=$BATS_TEST_TMPDIR/out
  run_lab "$LAB/procsub.sh" capture-released "$f"
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = incomplete ]
        assert_eventually "$f" hi
      else
        [ "$status" -eq 0 ]
        [[ $output == *[Ss]yntax* ]]
        [ "${lines[${#lines[@]} - 1]}" = incomplete ]
      fi
      ;;
    zsh | zsh-sh | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = incomplete ]
      assert_eventually "$f" hi
      ;;
    ksh93)
      if old_ksh; then
        assert_old_ksh_redirect_bug
      else
        [ "$status" -eq 0 ]
        [ "$output" = incomplete ]
        assert_eventually "$f" hi
      fi
      ;;
    yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "chain: in bash and ksh93 the second >(...) writes into the first" {
  local got f=$BATS_TEST_TMPDIR/first
  run_lab "$LAB/procsub.sh" chain "$f"
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = file=out:err:E ]
      else
        assert_syntax_error
      fi
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'err:E\nfile=')" ]
      ;;
    busybox)
      # The second consumer writes to the script's stdout, not into the first consumer.
      # Its line lands before or after the script's own line depending on the run.
      [ "$status" -eq 0 ]
      got=$(sorted_lines)
      [ "$got" = "$(printf 'err:E\nfile=')" ]
      ;;
    ksh93)
      if old_ksh; then
        assert_old_ksh_redirect_bug
      else
        [ "$status" -eq 0 ]
        [ "$output" = file=out:err:E ]
      fi
      ;;
    yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "stdin: the substituted process inherits the stdin of the process that expands it" {
  run_lab "$LAB/procsub.sh" stdin
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = in ]
      else
        assert_syntax_error
      fi
      ;;
    ksh93 | busybox | yash)
      [ "$status" -eq 0 ]
      [ "$output" = in ]
      ;;
    zsh | zsh-sh)
      # zsh expands the last pipeline stage in the parent shell, whose stdin is /dev/null.
      [ "$status" -eq 0 ]
      [ "$output" = "" ]
      ;;
    dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "lifetime: the pathname is dead once the command that created it returns" {
  local want
  want=$(support)
  run_lab "$LAB/procsub.sh" lifetime
  case $want in
    path | redirection)
      [ "$status" -eq 0 ]
      [ "$output" = dead ]
      ;;
    none) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "persist 5: exec moves the pipe to a descriptor that outlives the command" {
  local want
  want=$(support)
  run_lab "$LAB/procsub.sh" persist 5
  case $want in
    path)
      [ "$status" -eq 0 ]
      [ "$output" = line=persistent ]
      ;;
    redirection | none) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "persist 3: ksh93 fails when the target is the substitution's own descriptor" {
  run_lab "$LAB/procsub.sh" persist 3
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = line=persistent ]
      else
        assert_syntax_error
      fi
      ;;
    zsh | zsh-sh | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = line=persistent ]
      ;;
    ksh93)
      # ksh93 put the pipe on /dev/fd/3, the lowest free descriptor, and "3<" closes it first.
      [[ $output == *"3: cannot open [Bad file descriptor]"* ]]
      [[ $output == *line=unread* ]]
      ;;
    yash | dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "loopvar: a loop fed by < <(...) runs in the current shell" {
  local want
  want=$(support)
  run_lab "$LAB/procsub.sh" loopvar
  case $want in
    path)
      [ "$status" -eq 0 ]
      [ "$output" = n=2 ]
      ;;
    redirection | none) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "sigpipe: a producer nobody reads dies of SIGPIPE, reported per shell" {
  local f=$BATS_TEST_TMPDIR/st
  run_lab "$LAB/procsub.sh" sigpipe "$f"
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = producer=141 ]
      else
        assert_syntax_error
      fi
      ;;
    zsh | zsh-sh | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = producer=141 ]
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = producer=269 ]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [ "$output" = producer=397 ]
      ;;
    dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "count: 70 substitutions in one command get 70 distinct pathnames" {
  local want
  want=$(support)
  run_lab "$LAB/procsub.sh" count
  case $want in
    path)
      [ "$status" -eq 0 ]
      [ "$output" = "count=70 distinct=70" ]
      ;;
    redirection)
      [ "$status" -eq 0 ]
      [ "$output" = "count=0 distinct=0" ]
      ;;
    none) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "split: BusyBox ash field-splits the pathname; bash, zsh, and ksh93 do not" {
  run_lab "$LAB/procsub.sh" split
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = 1 ]
      else
        assert_syntax_error
      fi
      ;;
    zsh | zsh-sh | ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = 1 ]
      ;;
    busybox)
      # "/dev/fd/64" split on "/" gives "", "dev", "fd", "64".
      [ "$status" -eq 0 ]
      [ "$output" = 4 ]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [ "$output" = 0 ]
      ;;
    dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "quoted: quoting the operator makes it plain text in every shell" {
  run_lab "$LAB/procsub.sh" quoted
  [ "$status" -eq 0 ]
  [ "$output" = "<(:) >(:)" ]
}

@test "option: inside a word, ksh93 makes a separate argument and yash makes none" {
  local prefix
  run_lab "$LAB/procsub.sh" option
  case $(lab_shell_family) in
    bash)
      if bash_procsub_on; then
        [ "$status" -eq 0 ]
        [ "$output" = --file=/dev/fd/63 ]
      else
        # POSIX mode before 5.1 keeps "<(" inside a word as literal text.
        [ "$status" -eq 0 ]
        [ "$output" = "--file=<(:)" ]
      fi
      ;;
    busybox)
      [ "$status" -eq 0 ]
      [ "$output" = --file=/dev/fd/64 ]
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = "--file= /dev/fd/3" ]
      ;;
    zsh | zsh-sh)
      prefix=$(fd_prefix)
      [ "$status" -eq 0 ]
      [[ $output == "--file=$prefix/"[0-9]* ]]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [ "$output" = --file= ]
      ;;
    dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "source: bash 3.2 dots a substitution and runs nothing" {
  run_lab "$LAB/procsub.sh" source
  case $(lab_shell_family) in
    bash)
      if ! bash_procsub_on; then
        assert_syntax_error
      elif bash_at_least 4 0; then
        [ "$status" -eq 0 ]
        [ "$output" = "$(printf 'sourced\nrc=0')" ]
      else
        # bash 3.2 reads st_size bytes, and a pipe's st_size is 0.
        [ "$status" -eq 0 ]
        [ "$output" = rc=0 ]
      fi
      ;;
    zsh | zsh-sh | ksh93 | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf 'sourced\nrc=0')" ]
      ;;
    yash)
      [ "$status" -eq 2 ]
      [[ $output == *operand* ]]
      ;;
    dash | posh | mksh) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "errexit: set -e never sees a failing producer" {
  local want
  want=$(support)
  run_lab "$LAB/procsub.sh" errexit
  case $want in
    path | redirection)
      [ "$status" -eq 0 ]
      [ "$output" = survived ]
      ;;
    none) assert_syntax_error ;;
    *) false ;;
  esac
}

@test "orphan: the script returns before its producer finishes" {
  local want f=$BATS_TEST_TMPDIR/late
  want=$(support)
  run_lab "$LAB/procsub.sh" orphan "$f"
  case $want in
    path | redirection)
      [ "$status" -eq 0 ]
      [ "$output" = returned ]
      [ ! -e "$f" ]
      assert_eventually "$f" late
      ;;
    none) assert_syntax_error ;;
    *) false ;;
  esac
}

# The worked example: a producer failure that comm cannot see, and the checked version.

make_inputs() {
  printf 'b\na\n' >"$BATS_TEST_TMPDIR/old"
  printf 'c\na\nb\nd\n' >"$BATS_TEST_TMPDIR/new"
}

# assert_needs_bash: every shell but bash 5.1+ stops at the version guard.
assert_needs_bash() {
  [ "$status" -eq 3 ]
  [ "$output" = "added: needs bash 5.1 or later" ]
}

# modern_bash: the shell is bash 5.1 or later.
modern_bash() {
  local family
  family=$(lab_shell_family)
  [ "$family" = bash ] && bash_at_least 5 1
}

@test "added naive: both inputs readable, prints the new lines" {
  make_inputs
  run_lab "$LAB/added.sh" naive "$BATS_TEST_TMPDIR/old" "$BATS_TEST_TMPDIR/new"
  if modern_bash; then
    [ "$status" -eq 0 ]
    [ "$output" = "$(printf 'c\nd')" ]
  else
    assert_needs_bash
  fi
}

@test "added naive: a missing input succeeds with wrong output" {
  local got
  make_inputs
  run_lab "$LAB/added.sh" naive "$BATS_TEST_TMPDIR/missing" "$BATS_TEST_TMPDIR/new"
  if modern_bash; then
    [ "$status" -eq 0 ]
    # stdout and stderr interleave in any order, so compare sorted lines.
    got=$(sorted_lines)
    [ "$got" = "$(printf '%s\n' a "added: cannot read $BATS_TEST_TMPDIR/missing" b c d)" ]
  else
    assert_needs_bash
  fi
}

@test "added checked: both inputs readable, prints the new lines" {
  make_inputs
  run_lab "$LAB/added.sh" checked "$BATS_TEST_TMPDIR/old" "$BATS_TEST_TMPDIR/new"
  if modern_bash; then
    [ "$status" -eq 0 ]
    [ "$output" = "$(printf 'c\nd')" ]
  else
    assert_needs_bash
  fi
}

@test "added checked: a missing input fails with the producer's status" {
  local got
  make_inputs
  run_lab "$LAB/added.sh" checked "$BATS_TEST_TMPDIR/missing" "$BATS_TEST_TMPDIR/new"
  if modern_bash; then
    [ "$status" -eq 2 ]
    got=$(sorted_lines)
    [ "$got" = "$(printf '%s\n' a "added: cannot read $BATS_TEST_TMPDIR/missing" "added: producer failed (old=66 new=0)" b c d)" ]
  else
    assert_needs_bash
  fi
}
