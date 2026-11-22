#!/usr/bin/env bats
# stdio buffering on pipes, what stdbuf can and cannot change, and the
# utility flags that make a filter forward each line.
# lagcheck.sh prints "early=N total=M": early=6 means the stage forwarded the
# 6-byte line "hello\n" while its input was still open; early=0 means it held it.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  LAG="$LAB/lagcheck.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

# build NAME: compile NAME.c into the test's scratch directory; a missing
# compiler fails the test.
build() {
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -o "$BATS_TEST_TMPDIR/$1" "$LAB/$1.c"
}

# impl_of CMD: print gnu, bsd, busybox, uutils, or missing for a utility.
# The shell under test never changes which utility runs, so utility tests
# branch on this, not on lab_shell_family.
impl_of() {
  local path real out
  path=$(command -v "$1") || {
    echo missing
    return 0
  }
  real=$(readlink -f "$path" 2>/dev/null) || real=$path
  case $real in
    *busybox*)
      echo busybox
      return 0
      ;;
    *) ;;
  esac
  out=$("$1" --version 2>&1 </dev/null) || true
  case $out in
    *uutils*) echo uutils ;;
    *"BSD grep"*) echo bsd ;;
    *GNU*) echo gnu ;;
    *) echo bsd ;;
  esac
}

# libc_of: print glibc, musl, or apple for the C library the probes link.
libc_of() {
  case $(uname -s) in
    Darwin) echo apple ;;
    Linux)
      if getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
        echo glibc
      else
        echo musl
      fi
      ;;
    *) echo unknown ;;
  esac
}

@test "a stdio filter holds a line it already copied while stdout is a pipe, and exit flushes it" {
  build pipeprobe
  build chatter
  run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd "$BATS_TEST_TMPDIR/chatter" plain
  [ "$status" -eq 0 ]
  [ "$output" = "early=0 total=6" ]
}

@test "stdbuf -oL and -o0 make the same filter forward each line, including in grandchildren" {
  build pipeprobe
  build chatter
  run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd stdbuf -oL "$BATS_TEST_TMPDIR/chatter" plain
  [ "$status" -eq 0 ]
  [ "$output" = "early=6 total=6" ]
  run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd stdbuf -o0 "$BATS_TEST_TMPDIR/chatter" plain
  [ "$status" -eq 0 ]
  [ "$output" = "early=6 total=6" ]
  # The settings travel in the environment, so a grandchild is affected too.
  # shellcheck disable=SC2016 # $1 belongs to the inner sh
  run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd stdbuf -oL sh -c '"$1" plain; :' sh "$BATS_TEST_TMPDIR/chatter"
  [ "$status" -eq 0 ]
  [ "$output" = "early=6 total=6" ]
}

@test "_STDBUF_O=L without stdbuf works only where libc itself reads it" {
  build pipeprobe
  build chatter
  run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd env _STDBUF_O=L "$BATS_TEST_TMPDIR/chatter" plain
  [ "$status" -eq 0 ]
  case $(libc_of) in
    apple) [ "$output" = "early=6 total=6" ] ;;        # stdio/FreeBSD/makebuf.c __senvbuf
    glibc | musl) [ "$output" = "early=0 total=6" ] ;; # only libstdbuf reads it
    *) false ;;
  esac
}

@test "a program that calls setvbuf itself overrides stdbuf" {
  build pipeprobe
  build chatter
  run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd stdbuf -oL "$BATS_TEST_TMPDIR/chatter" own
  [ "$status" -eq 0 ]
  [ "$output" = "early=0 total=6" ]
}

@test "cat uses read and write directly, so it forwards at once with no stdbuf" {
  build pipeprobe
  run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd /bin/cat
  [ "$status" -eq 0 ]
  [ "$output" = "early=6 total=6" ]
}

@test "a shell read and printf loop forwards each line in every shell" {
  build pipeprobe
  run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" builtin
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox)
      [ "$output" = "early=6 total=6" ]
      ;;
    *) false ;;
  esac
}

@test "grep holds matches on a pipe; --line-buffered forwards them where it exists" {
  build pipeprobe
  impl=$(impl_of grep)
  case $impl in
    gnu | bsd)
      run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd grep hello
      [ "$status" -eq 0 ]
      [ "$output" = "early=0 total=6" ]
      run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd grep --line-buffered hello
      [ "$status" -eq 0 ]
      [ "$output" = "early=6 total=6" ]
      ;;
    busybox)
      # BusyBox grep has no --line-buffered option (findutils/grep.c).
      run_lab_c 'grep --line-buffered hello </dev/null'
      [ "$status" -ne 0 ]
      [ "$status" -ne 1 ]
      ;;
    *) false ;;
  esac
}

@test "sed holds output on a pipe; sed -u forwards it where it exists" {
  build pipeprobe
  impl=$(impl_of sed)
  case $impl in
    gnu | bsd)
      run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd sed -n p
      [ "$status" -eq 0 ]
      [ "$output" = "early=0 total=6" ]
      run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd sed -u -n p
      [ "$status" -eq 0 ]
      [ "$output" = "early=6 total=6" ]
      ;;
    busybox)
      # BusyBox sed accepts only -i -r -E -n -e -f (editors/sed.c getopt32long).
      run_lab_c 'sed -u -n p </dev/null'
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "awk needs fflush() to forward a record, and mawk also needs -W interactive" {
  build pipeprobe
  found=0
  # shellcheck disable=SC2312 # lab_awks only lists installed awks; a missing awk shows up as found=0
  while IFS= read -r awk_cmd; do
    found=1
    # shellcheck disable=SC2086,SC2248 # "busybox awk" is a command plus an argument
    flavor=$(lab_awk_flavor $awk_cmd)
    # shellcheck disable=SC2086,SC2248 # same
    run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd $awk_cmd '{ print }'
    [ "$status" -eq 0 ]
    [ "$output" = "early=0 total=6" ]
    # shellcheck disable=SC2086,SC2248 # same
    run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd $awk_cmd '{ print; fflush() }'
    [ "$status" -eq 0 ]
    case $flavor in
      gawk | onetrue | busybox) [ "$output" = "early=6 total=6" ] ;;
      mawk)
        # fin.c fillbuff() reads until its buffer is full or EOF, so the
        # record is never parsed early and fflush() has nothing to flush.
        [ "$output" = "early=0 total=6" ]
        # shellcheck disable=SC2086,SC2248 # same
        run_lab "$LAG" "$BATS_TEST_TMPDIR/pipeprobe" cmd $awk_cmd -W interactive '{ print }'
        [ "$status" -eq 0 ]
        [ "$output" = "early=6 total=6" ]
        ;;
      *) false ;;
    esac
  done <<EOF
$(lab_awks)
EOF
  [ "$found" -eq 1 ]
}

@test "a process killed by a signal loses its stdio buffer; exit at EOF does not" {
  build chatter
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c 'printf "hello\n" | "$1" die | wc -c | tr -d " "' sh "$BATS_TEST_TMPDIR/chatter"
  [ "$status" -eq 0 ]
  [ "$output" = 0 ]
  # shellcheck disable=SC2016 # same
  run_lab_c 'printf "hello\n" | "$1" plain | wc -c | tr -d " "' sh "$BATS_TEST_TMPDIR/chatter"
  [ "$status" -eq 0 ]
  [ "$output" = 6 ]
}

@test "stderr overtakes stdout when both go into one pipe" {
  build chatter
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c '"$1" order 2>&1 | /bin/cat' sh "$BATS_TEST_TMPDIR/chatter"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'err\nout')" ]
}

@test "libc picks the stdout buffer size on a pipe, and stdbuf replaces it" {
  build bufsize
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c '"$1" stdout 3>&1' sh "$BATS_TEST_TMPDIR/bufsize"
  [ "$status" -eq 0 ]
  case $(libc_of) in
    glibc) [ "$output" = "mode=full size=4096" ] ;;  # min(BUFSIZ 8192, st_blksize 4096)
    apple) [ "$output" = "mode=full size=16384" ] ;; # st_blksize of a pipe
    musl) [ "$output" = "mode=full size=1024" ] ;;   # static BUFSIZ buffer
    *) false ;;
  esac
  # shellcheck disable=SC2016 # same
  run_lab_c 'stdbuf -o 1000 "$1" stdout 3>&1' sh "$BATS_TEST_TMPDIR/bufsize"
  [ "$status" -eq 0 ]
  case $(libc_of) in
    glibc | apple) [ "$output" = "mode=full size=1000" ] ;;
    musl) [ "$output" = "mode=full size=992" ] ;; # setvbuf keeps UNGET (8) bytes
    *) false ;;
  esac
  # shellcheck disable=SC2016 # same
  run_lab_c 'stdbuf -oL "$1" stdout 3>&1' sh "$BATS_TEST_TMPDIR/bufsize"
  [ "$status" -eq 0 ]
  case $(libc_of) in
    glibc) [ "$output" = "mode=line size=4096" ] ;;
    apple) [ "$output" = "mode=line size=1024" ] ;; # __smakebuf falls back to BUFSIZ
    musl) [ "$output" = "mode=line size=1024" ] ;;
    *) false ;;
  esac
}

@test "stderr starts unbuffered" {
  build bufsize
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c '"$1" stderr 3>&1' sh "$BATS_TEST_TMPDIR/bufsize"
  [ "$status" -eq 0 ]
  case $(libc_of) in
    glibc | apple | musl) [ "$output" = "mode=unbuffered" ] ;;
    *) false ;;
  esac
}

@test "a buffered reader on a pipe takes more input than it uses; on a file it seeks back" {
  printf '1\n2\n3\n' >"$BATS_TEST_TMPDIR/f"
  # POSIX.1-2024 XCU 1.4 INPUT FILES: a seekable file must be left just past the last byte used.
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c '(sed -n 1q; /bin/cat) <"$1"' sh "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  case $(impl_of sed) in
    gnu | bsd | busybox) [ "$output" = "$(printf '2\n3')" ] ;;
    *) false ;;
  esac
  # On a pipe the offset is unspecified; these seds read a whole block.
  # shellcheck disable=SC2016 # same
  run_lab_c '/bin/cat "$1" | (sed -n 1q; /bin/cat)' sh "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  case $(impl_of sed) in
    gnu | bsd | busybox) [ "$output" = "" ] ;;
    *) false ;;
  esac
  # The shell's read builtin reads a pipe one byte at a time.
  # shellcheck disable=SC2016 # same
  run_lab_c '/bin/cat "$1" | { read -r x; /bin/cat; }' sh "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox)
      [ "$output" = "$(printf '2\n3')" ]
      ;;
    *) false ;;
  esac
}

@test "stdbuf -i0 stops a stdio reader at the line it needs, but not a read(2) reader" {
  printf '1\n2\n3\n' >"$BATS_TEST_TMPDIR/f"
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c '/bin/cat "$1" | (stdbuf -i0 sed -n 1q; /bin/cat)' sh "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  case $(impl_of sed) in
    gnu | bsd | busybox) [ "$output" = "$(printf '2\n3')" ] ;;
    *) false ;;
  esac
  # shellcheck disable=SC2016 # same
  run_lab_c '/bin/cat "$1" | (stdbuf -i0 head -n 1 >/dev/null; /bin/cat)' sh "$BATS_TEST_TMPDIR/f"
  [ "$status" -eq 0 ]
  case $(impl_of head) in
    gnu) [ "$output" = "" ] ;;                           # GNU head does not read through stdio
    bsd | busybox) [ "$output" = "$(printf '2\n3')" ] ;; # getline/getc on stdin
    *) false ;;
  esac
}

@test "stdbuf exports _STDBUF_O and reports failures with implementation-specific statuses" {
  run_lab_c 'stdbuf -oL env | grep "^_STDBUF_O="'
  [ "$status" -eq 0 ]
  [ "$output" = "_STDBUF_O=L" ]
  impl=$(impl_of stdbuf)
  run_lab_c 'stdbuf -oL ./no-such-command-here 2>/dev/null'
  case $impl in
    gnu | uutils) [ "$status" -eq 127 ] ;;
    bsd) [ "$status" -eq 2 ] ;; # err(2, ...) after execvp fails
    *) false ;;
  esac
  run_lab_c 'stdbuf -iL true 2>/dev/null'
  case $impl in
    gnu | uutils) [ "$status" -eq 125 ] ;; # "line buffering stdin is meaningless"
    bsd) [ "$status" -eq 0 ] ;;
    *) false ;;
  esac
}
