#!/usr/bin/env bats
# Tests for the piped-installer lab: what a shell does with a script that arrives on a pipe.
# Every script runs through run_lab so the matrix can swap the shell (LAB_SHELL).

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  TMPDIR="$BATS_TEST_TMPDIR"
  export TMPDIR
  lab_shell_words
}

# cut_after FILE MARKER: byte count of FILE up to and including the first MARKER (ASCII files only).
cut_after() {
  local text pre
  text=$(cat "$1")
  pre=${text%%"$2"*}
  printf '%s\n' $((${#pre} + ${#2}))
}

# last_line: the last line of $output.
last_line() {
  printf '%s\n' "${output##*$'\n'}"
}

@test "fake-curl: ok prints the whole file, cut prints a prefix and exits 18, fail exits 22" {
  run_lab "$LAB/fake-curl.sh" ok "$LAB/installer-unwrapped.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "$(cat "$LAB/installer-unwrapped.sh")" ]
  run_lab "$LAB/fake-curl.sh" cut "$LAB/installer-unwrapped.sh" 5
  [ "$status" -eq 18 ]
  [ "$output" = '#!/bi' ]
  run_lab "$LAB/fake-curl.sh" fail
  [ "$status" -eq 22 ]
  [ -z "$output" ]
}

@test "sha256-of prints the known digest of 'abc'" {
  printf abc >"$BATS_TEST_TMPDIR/abc"
  run_lab "$LAB/sha256-of.sh" "$BATS_TEST_TMPDIR/abc"
  [ "$status" -eq 0 ]
  [ "$output" = ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad ]
}

@test "verify-then-run runs the installer only when the pinned digest matches" {
  local want
  want=$("$LAB/sha256-of.sh" "$LAB/installer-wrapped.sh")
  run_lab "$LAB/verify-then-run.sh" "$LAB/installer-wrapped.sh" "$want"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'install: begin\nREMOVE /opt/tool/cache\ninstall: done')" ]
  run_lab "$LAB/verify-then-run.sh" "$LAB/installer-wrapped.sh" 0000000000000000000000000000000000000000000000000000000000000000
  [ "$status" -eq 1 ]
  [[ $output == "refusing to run: sha256 $want does not match pinned 000"* ]]
  [[ $output != *"install: begin"* ]]
}

@test "stdin-reader: the script's own read takes the next script line unless the shell read ahead" {
  run_lab "$LAB/feed.sh" "$(cat "$LAB/stdin-reader.sh")" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  local posix='line=[echo executed-by-the-shell]'
  local ahead
  ahead=$(printf 'executed-by-the-shell\nline=[]')
  case $(lab_shell_family) in
    # POSIX XCU sh STDIN: the shell must not read ahead of a command that reads standard input
    bash | zsh | zsh-sh | ksh93 | mksh | yash | posh) [ "$output" = "$posix" ] ;;
    # BusyBox ash 1.37.0 reads a block from a pipe (measured and read in source)
    busybox) [ "$output" = "$ahead" ] ;;
    # dash 0.5.12 reads a block, dash 0.5.13 and later do not; the family cannot tell them apart
    dash) [ "$output" = "$posix" ] || [ "$output" = "$ahead" ] ;;
    *) false ;;
  esac
}

@test "a script piped to sh -s sees its arguments, and standard input is not a terminal" {
  run_lab "$LAB/feed.sh" 'echo "args=$1,$2 n=$#"' "${LAB_SHELL_WORDS[@]}" -s -- a b
  case $(lab_shell_family) in
    # measured: posh 0.14.1 has no -s option, so `posh -s -- a b` is an error and the script never runs
    posh)
      [ "$status" -eq 1 ]
      [[ $output == *"-s: unknown option"* ]]
      ;;
    *)
      [ "$status" -eq 0 ]
      [ "$output" = 'args=a,b n=2' ]
      ;;
  esac
  run_lab "$LAB/feed.sh" 'if [ -t 0 ]; then echo tty; else echo not-tty; fi' "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  [ "$output" = not-tty ]
}

@test "a command that reads standard input eats the rest of the script unless it gets its own stdin" {
  # cat reads to end of file, so the next script line is consumed as data and never runs
  run_lab "$LAB/feed.sh" "$(printf 'echo start\ncat >/dev/null\necho after-cat')" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | ksh93 | mksh | yash | posh) [ "$output" = start ] ;;
    # BusyBox ash read ahead and already holds the next line
    busybox) [ "$output" = "$(printf 'start\nafter-cat')" ] ;;
    # dash 0.5.12 read ahead, dash 0.5.13 and later do not
    dash) [ "$output" = start ] || [ "$output" = "$(printf 'start\nafter-cat')" ] ;;
    *) false ;;
  esac
  # the fix: give the command its own stdin
  run_lab "$LAB/feed.sh" "$(printf 'echo start\ncat </dev/null\necho after-cat')" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'start\nafter-cat')" ]
}

@test "unwrapped installer: every cut inside the path runs remove on a prefix of the path" {
  run_lab "$LAB/truncate-sweep.sh" "$LAB/installer-unwrapped.sh" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  local full=/opt/tool/cache targets prefix t i=1
  targets=$(printf '%s\n' "$output" | awk -F'\t' '$2 != "-" { print $2 }')
  while [ "$i" -lt "${#full}" ]; do
    prefix=${full:0:i}
    printf '%s\n' "$targets" | grep -qxF -- "$prefix"
    i=$((i + 1))
  done
  printf '%s\n' "$targets" | grep -qxF -- "$full"
  while IFS= read -r t; do
    case $full in
      "$t"*) ;;
      *) false ;;
    esac
  done <<<"$targets"
}

@test "wrapped installer: only a cut inside the final main line runs, and it runs the whole body" {
  run_lab "$LAB/truncate-sweep.sh" "$LAB/installer-wrapped.sh" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  local ran
  ran=$(printf '%s\n' "$output" | awk -F'\t' '$2 != "-"' | wc -l)
  ran=${ran##* }
  # a cut after "main", after "main ", and after the closing quote; ksh93u+ 2012 also accepts "main \"$"
  [ "$ran" -ge 3 ]
  [ "$ran" -le 4 ]
  [ -z "$(printf '%s\n' "$output" | awk -F'\t' '$2 != "-" && $2 != "/opt/tool/cache"')" ]
}

@test "piped install: a cut download runs a partial installer and the pipeline status hides curl's 18" {
  local n
  n=$(cut_after "$LAB/installer-unwrapped.sh" 'remove /opt')
  run_lab "$LAB/fetch-modes.sh" pipe "$LAB/installer-unwrapped.sh" "$n" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'install: begin\nREMOVE /opt\nstatus=0')" ]
}

@test "pipefail reports curl's 18, but only after the partial installer has already run" {
  local n expected
  n=$(cut_after "$LAB/installer-unwrapped.sh" 'remove /opt')
  # bats `run` always returns 0, so read $status to learn whether the shell accepts the option
  run_lab_c 'set -o pipefail'
  if [ "$status" -eq 0 ]; then
    expected=$(printf 'install: begin\nREMOVE /opt\nstatus=18')
  else
    expected=pipefail=unsupported
  fi
  run_lab "$LAB/fetch-modes.sh" pipefail "$LAB/installer-unwrapped.sh" "$n" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  [ "$output" = "$expected" ]
}

@test "eval of a command substitution also runs the partial installer and masks the status" {
  local n
  n=$(cut_after "$LAB/installer-unwrapped.sh" 'remove /opt')
  run_lab "$LAB/fetch-modes.sh" eval "$LAB/installer-unwrapped.sh" "$n" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'install: begin\nREMOVE /opt\nstatus=0')" ]
}

@test "checking the download status before running refuses the cut installer" {
  local n
  n=$(cut_after "$LAB/installer-unwrapped.sh" 'remove /opt')
  run_lab "$LAB/fetch-modes.sh" checked "$LAB/installer-unwrapped.sh" "$n" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 1 ]
  [ "$output" = fetch-failed=18 ]
}

@test "a cut inside the wrapped function body runs nothing and the shell reports an error" {
  local n
  n=$(cut_after "$LAB/installer-wrapped.sh" 'remove /opt')
  run_lab "$LAB/fetch-modes.sh" pipe "$LAB/installer-wrapped.sh" "$n" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  [[ $output != *REMOVE* ]]
  [[ $output != *"install: begin"* ]]
  [[ "$(last_line)" == status=* ]]
  [ "$(last_line)" != status=0 ]
}

@test "a truncated here-document: most shells write a short config file, mksh, posh and yash refuse it" {
  local n out="$BATS_TEST_TMPDIR/conf" want
  n=$(cut_after "$LAB/installer-heredoc.sh" 'data_dir = /var')
  export CONF_OUT="$out"
  run_lab "$LAB/fetch-modes.sh" pipe "$LAB/installer-heredoc.sh" "$n" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  want=$(printf 'listen = 127.0.0.1:8080\ndata_dir = /var')
  case $(lab_shell_family) in
    # measured on bash 3.2 and 5.3, dash 0.5.12 and 0.5.13, zsh 5.9 (native and sh), ksh93u+ 2012,
    # ksh93u+m 1.0.10 and BusyBox ash 1.37.0: the command ran on the partial text
    bash | zsh | zsh-sh | dash | ksh93 | busybox)
      [ -e "$out" ]
      [ "$(cat "$out")" = "$want" ]
      [ "$(last_line)" = status=0 ]
      ;;
    # measured: mksh R59c and posh 0.14.1 report "here document 'EOF' unclosed" and exit 1;
    # nothing is written
    mksh | posh)
      [ ! -e "$out" ]
      [ "$(last_line)" = status=1 ]
      ;;
    # measured: yash 2.58.1 reports a syntax error and exits 2; nothing is written
    yash)
      [ ! -e "$out" ]
      [ "$(last_line)" = status=2 ]
      ;;
    *) false ;;
  esac
}

@test "a stalled script stalls its producer: padding larger than the pipe leaves the writer blocked" {
  run_lab "$LAB/throttle-probe.sh" "$BATS_TEST_TMPDIR" 4194304 "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  [ "$output" = writer-blocked ]
}

@test "the same probe with padding that fits in the pipe sees the writer finish" {
  run_lab "$LAB/throttle-probe.sh" "$BATS_TEST_TMPDIR" 640 "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  [ "$output" = writer-finished ]
}
