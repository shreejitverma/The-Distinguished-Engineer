#!/usr/bin/env bats
# xtrace, PS4 and BASH_XTRACEFD: what each shell prints, where it writes it, and how bash
# versions differ. probe.sh runs under every shell; bash-probe.sh and trace-demo.sh need bash;
# writes.c counts the write(2) calls behind one trace line.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  BPROBE="$BATS_TEST_DIRNAME/bash-probe.sh"
  DEMO="$BATS_TEST_DIRNAME/trace-demo.sh"
  OUT="$BATS_TEST_TMPDIR/trace"
  FAMILY=$(lab_shell_family)
}

# bash_at_least MAJOR MINOR: succeed if the bash under test is at least MAJOR.MINOR.
bash_at_least() {
  local v major minor
  v=$(lab_shell_version)
  major=${v%%.*}
  minor=${v#*.}
  minor=${minor%%.*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# build_writes: compile writes.c into the test's scratch directory and set WRITES.
build_writes() {
  WRITES="$BATS_TEST_TMPDIR/writes"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -o "$WRITES" "$BATS_TEST_DIRNAME/writes.c"
}

@test "PS4 starts as '+ ' everywhere except native zsh, which uses '+%N:%i> '" {
  run_lab "$PROBE" default
  [ "$status" -eq 0 ]
  case $FAMILY in
    zsh) [ "$output" = "[+%N:%i> ]" ] ;;
    bash | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = "[+ ]" ] ;;
    *) false ;;
  esac
}

@test "PS4 is parameter-expanded before each trace line, except in native zsh and posh" {
  run_lab "$PROBE" expand "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh-sh | dash | ksh93 | mksh | busybox) [ "$output" = "[7] : marker" ] ;;
    yash) [ "$output" = "[7] ':' marker" ] ;;
    zsh | posh) [ "$output" = "[\$v] : marker" ] ;;
    *) false ;;
  esac
}

@test "a traced argument with a space is shell-quoted, except in dash and posh" {
  run_lab "$PROBE" quote "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | ksh93 | mksh | busybox) [ "$output" = "+ : marker 'c d'" ] ;;
    yash) [ "$output" = "+ ':' marker 'c d'" ] ;;
    dash | posh) [ "$output" = "+ : marker c d" ] ;;
    *) false ;;
  esac
}

@test "only bash repeats the first PS4 character for a trace from inside \$( )" {
  run_lab "$PROBE" nest "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = replicated ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = flat ] ;;
    *) false ;;
  esac
}

@test "set +x is traced by the Bourne lineage and not by ksh93 (POSIX leaves it unspecified)" {
  run_lab "$PROBE" setplusx "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | dash | mksh | yash | posh | busybox) [ "$output" = traced ] ;;
    ksh93) [ "$output" = untraced ] ;;
    *) false ;;
  esac
}

@test "{ set +x; } 2>/dev/null hides the set +x line, except in mksh, which writes to a saved descriptor" {
  run_lab "$PROBE" hide "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | dash | ksh93 | yash | posh | busybox) [ "$output" = hidden ] ;;
    mksh) [ "$output" = shown ] ;;
    *) false ;;
  esac
}

@test "a command's own 2>/dev/null does not hide its trace line, except in yash, which opens redirections first" {
  run_lab "$PROBE" ownredir "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | dash | ksh93 | mksh | posh | busybox) [ "$output" = shown ] ;;
    yash) [ "$output" = hidden ] ;;
    *) false ;;
  esac
}

@test "the trace follows a later exec 2>FILE, except in mksh, which snapshots stderr at set -x" {
  run_lab "$PROBE" follows "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | dash | ksh93 | yash | posh | busybox) [ "$output" = follows ] ;;
    mksh) [ "$output" = snapshot ] ;;
    *) false ;;
  esac
}

@test "set -x inside a function outlives the call, except in zsh and mksh, which restore it" {
  run_lab "$PROBE" funcscope "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | dash | ksh93 | yash | posh | busybox) [ "$output" = kept ] ;;
    zsh | zsh-sh | mksh) [ "$output" = restored ] ;;
    *) false ;;
  esac
}

@test "BASH_XTRACEFD moves the trace in bash 4.1 and later and in BusyBox ash, nowhere else" {
  run_lab "$PROBE" xtracefd "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 4 1; then
        [ "$output" = fd3 ]
      else
        [ "$output" = stderr ]
      fi
      ;;
    busybox) [ "$output" = fd3 ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh) [ "$output" = stderr ] ;;
    *) false ;;
  esac
}

@test "one short trace line is one write(2) in buffered shells and one write per word in dash, BusyBox, and yash on musl" {
  build_writes
  lab_shell_words
  # shellcheck disable=SC2016 # "$@" belongs to the shell under test
  run_lab_c '"$@"' sh "$WRITES" 2 "${LAB_SHELL_WORDS[@]}" -c 'PS4="+ "; set -x; : one two three'
  [ "$status" -eq 0 ]
  case $FAMILY in
    yash)
      # yash's setvbuf(stderr, NULL, _IOLBF, BUFSIZ) allocates no buffer under musl,
      # so each fprintf and fputc in print_xtrace is its own write.
      [ "$output" = "writes=9 bytes=20
[+ ]
[':']
[ ]
[one]
[ ]
[two]
[ ]
[three]
[\\n]" ]
      ;;
    bash | zsh | zsh-sh | ksh93 | mksh | posh)
      [ "$output" = "writes=1 bytes=18
[+ : one two three\\n]" ]
      ;;
    dash | busybox)
      [ "$output" = "writes=6 bytes=18
[+ ]
[:]
[ one]
[ two]
[ three]
[\\n]" ]
      ;;
    *) false ;;
  esac
}

@test "bash 4.1+: a BASH_XTRACEFD trace line is also a single write(2)" {
  build_writes
  lab_shell_words
  # shellcheck disable=SC2016 # "$@" belongs to the shell under test
  run_lab_c '"$@"' sh "$WRITES" 3 "${LAB_SHELL_WORDS[@]}" -c 'exec 2>/dev/null; BASH_XTRACEFD=3; PS4="+ "; set -x; : one two three'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 4 1; then
        [ "$output" = "writes=1 bytes=18
[+ : one two three\\n]" ]
      else
        [ "$output" = "writes=0 bytes=0" ]
      fi
      ;;
    busybox)
      [ "$output" = "writes=6 bytes=18
[+ ]
[:]
[ one]
[ two]
[ three]
[\\n]" ]
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh) [ "$output" = "writes=0 bytes=0" ] ;;
    *) false ;;
  esac
}

@test "bash: assigning a new BASH_XTRACEFD leaves the old descriptor open" {
  run_lab "$BPROBE" reassign "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "fd3=open
fd4=open" ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = needs-bash ] ;;
    *) false ;;
  esac
}

@test "bash 4.1+: unset BASH_XTRACEFD closes the descriptor it named" {
  run_lab "$BPROBE" unset "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 4 1; then
        [ "$output" = fd4=closed ]
      else
        [ "$output" = fd4=open ]
      fi
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = needs-bash ] ;;
    *) false ;;
  esac
}

@test "bash: closing the trace descriptor with exec N>&- sends the trace back to stderr" {
  run_lab "$BPROBE" closefd "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = stderr ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = needs-bash ] ;;
    *) false ;;
  esac
}

@test "bash 4.1+: a BASH_XTRACEFD that names no open descriptor is rejected with a diagnostic" {
  run_lab "$BPROBE" invalid "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 4 1; then
        [ "$output" = "BASH_XTRACEFD: 9: invalid value for trace file descriptor" ]
      else
        [ "$output" = "" ]
      fi
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = needs-bash ] ;;
    *) false ;;
  esac
}

@test "bash 4.1+: with BASH_XTRACEFD=3, { set +x; } 2>/dev/null leaks the set +x line and 3>/dev/null hides it" {
  run_lab "$BPROBE" silence "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 4 1; then
        [ "$output" = "+ : one
+ set +x
+ : two" ]
      else
        [ "$output" = "" ]
      fi
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = needs-bash ] ;;
    *) false ;;
  esac
}

@test "bash: the first PS4 character repeats once per eval level, the rest of PS4 does not" {
  run_lab "$BPROBE" replicate "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = ">x eval ': e1; eval \": e2\"'
>>x : e1
>>x eval ': e2'
>>>x : e2" ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = needs-bash ] ;;
    *) false ;;
  esac
}

@test "bash 5.3 traces a control character as \$'...', older bash inside single quotes" {
  run_lab "$BPROBE" ctrl "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 5 3; then
        [ "$output" = "+ : \$'a\\tb'" ]
      else
        [ "$output" = "+ : 'a<TAB>b'" ]
      fi
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = needs-bash ] ;;
    *) false ;;
  esac
}

@test "bash: a scalar assignment is traced quoted, an array assignment as written" {
  run_lab "$BPROBE" assign "$OUT"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "+ eval 'v=\"a b\"; arr=(x \"y z\")'
++ v='a b'
++ arr=(x \"y z\")" ] ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox) [ "$output" = needs-bash ] ;;
    *) false ;;
  esac
}

@test "trace-demo.sh: the trace goes to its own log with file, line and function, and the secret stays out" {
  local log="$BATS_TEST_TMPDIR/trace.log"
  : >"$log"
  local logged
  run_lab "$DEMO" "$log"
  logged=$(cat "$log")
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      if bash_at_least 4 1; then
        [ "$output" = "fetching https://example.invalid/data
warning: cache cold" ]
        [ "$logged" = "+ trace-demo.sh:27:main: fetch https://example.invalid/data
+ trace-demo.sh:23:fetch: load_token
+ trace-demo.sh:24:fetch: printf 'fetching %s\n' https://example.invalid/data
+ trace-demo.sh:28:main: echo 'warning: cache cold'" ]
      else
        [ "$output" = "+ trace-demo.sh:27:main: fetch https://example.invalid/data
+ trace-demo.sh:23:fetch: load_token
+ trace-demo.sh:24:fetch: printf 'fetching %s\n' https://example.invalid/data
fetching https://example.invalid/data
+ trace-demo.sh:28:main: echo 'warning: cache cold'
warning: cache cold" ]
        [ "$logged" = "" ]
      fi
      run grep -F -e example-secret "$log"
      [ "$status" -eq 1 ]
      ;;
    zsh | zsh-sh | dash | ksh93 | mksh | yash | posh | busybox)
      [ "$status" -eq 2 ]
      [ "$output" = "trace-demo.sh: needs bash" ]
      ;;
    *) false ;;
  esac
}
