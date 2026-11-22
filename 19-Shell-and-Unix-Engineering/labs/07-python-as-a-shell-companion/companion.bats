#!/usr/bin/env bats
# The shell side of a Python companion: how program text, arguments, data,
# signal dispositions, and exit statuses cross the boundary into python3.
# The matrix images have no python3, so pyshim.c reproduces the interpreter's
# side of each contract and fakepy stands in for the interpreter itself.

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  SHIM="$BATS_TEST_TMPDIR/pyshim"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -o "$SHIM" "$LAB/pyshim.c"
  ln -s pyshim "$BATS_TEST_TMPDIR/fakepy"
  CSVCOL="$LAB/csvcol.sh"
  export PYTHON="$BATS_TEST_TMPDIR/fakepy"
  CSVCOL_BODY=$(awk '/^EOF$/ { p = 0 } p { print } /3<<.EOF.$/ { p = 1 }' "$CSVCOL")
}

@test "a quoted here-document delimiter hands dollar, backtick and backslash through unchanged" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'v=EXPANDED
cat <<\EOF
print("$v `date` \\ \$")
EOF
cat <<EOF
print("$v \\ \$")
EOF'
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # the expected text is literal on purpose
  [ "${lines[0]}" = 'print("$v `date` \\ \$")' ]
  [ "${lines[1]}" = 'print("EXPANDED \ $")' ]
}

@test "<<- strips every leading tab, so an indented Python block loses its structure" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 't=$(printf "\t")
eval "cat <<-EOF
${t}if True:
${t}${t}print(1)
${t}EOF
cat <<EOF
${t}if True:
${t}${t}print(1)
EOF
"'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'if True:\nprint(1)\n\tif True:\n\t\tprint(1)')" ]
}

@test "a here-document on standard input displaces piped data, except under zsh MULTIOS" {
  run_lab_c 'printf "data\n" | cat <<\EOF
program
EOF'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh) [ "$output" = "$(printf 'data\nprogram')" ] ;; # MULTIOS concatenates both inputs
    bash | zsh-sh | dash | posh | mksh | ksh93 | yash | busybox) [ "$output" = program ] ;;
    *) false ;;
  esac
}

@test "a program on fd 3 leaves standard input free for the data" {
  run_lab_c 'printf "data\n" | cat /dev/fd/3 - 3<<\EOF
program
EOF'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'program\ndata')" ]
}

@test "a value spliced into program text runs as code, in every awk here" {
  payload='x"; system("touch pwned"); print "'
  awks=$(lab_awks)
  [ -n "$awks" ]
  while IFS= read -r awk_cmd; do
    read -r -a awk_words <<<"$awk_cmd"
    rm -f "$BATS_TEST_TMPDIR/pwned"
    # shellcheck disable=SC2016 # the expansions belong to the shell under test
    run_lab_c 'cd "$1" || exit 1; v=$2; shift 2; "$@" "BEGIN { print \"$v\" }"' \
      sh "$BATS_TEST_TMPDIR" "$payload" "${awk_words[@]}" </dev/null
    [ "$status" -eq 0 ]
    [ -e "$BATS_TEST_TMPDIR/pwned" ]
  done <<<"$awks"
}

@test "the same value passed in argv stays data, in every awk here" {
  payload='x"; system("touch pwned"); print "'
  awks=$(lab_awks)
  [ -n "$awks" ]
  while IFS= read -r awk_cmd; do
    read -r -a awk_words <<<"$awk_cmd"
    # shellcheck disable=SC2016 # the expansions belong to the shell under test
    run_lab_c 'cd "$1" || exit 1; v=$2; shift 2; "$@" "BEGIN { print ARGV[1] }" "$v"' \
      sh "$BATS_TEST_TMPDIR" "$payload" "${awk_words[@]}" </dev/null
    [ "$status" -eq 0 ]
    [ "$output" = "$payload" ]
    [ ! -e "$BATS_TEST_TMPDIR/pwned" ]
  done <<<"$awks"
}

@test "an ignored SIGPIPE survives exec, as it does for children of os.system and os.exec" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '"$1" sigstate; "$1" ignpipe "$1" sigstate' sh "$SHIM"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'SIGPIPE=default\nSIGPIPE=ignored')" ]
}

@test "with SIGPIPE ignored a writer to a closed pipe gets EPIPE instead of dying" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '"$1" pipe; "$1" ignpipe "$1" pipe' sh "$SHIM"
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'writer killed by signal 13\nwriter got EPIPE')" ]
}

@test "a shell started with SIGPIPE ignored cannot reset it with trap, except zsh" {
  lab_shell_words
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'shim=$1; shift; exec "$shim" ignpipe "$@"' sh "$SHIM" \
    "${LAB_SHELL_WORDS[@]}" -c 'trap - PIPE; "$0" sigstate; "$0" pipe' "$SHIM"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh | zsh-sh) [ "$output" = "$(printf 'SIGPIPE=default\nwriter killed by signal 13')" ] ;;
    bash | dash | posh | mksh | ksh93 | yash | busybox)
      [ "$output" = "$(printf 'SIGPIPE=ignored\nwriter got EPIPE')" ]
      ;;
    *) false ;;
  esac
}

@test "the exit status keeps only the low eight bits of the value given to exit" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '"$1" exit 256; a=$?; "$1" exit 257; b=$?; "$1" exit -1; echo "$a $b $?"' sh "$SHIM"
  [ "$status" -eq 0 ]
  [ "$output" = "0 1 255" ]
}

@test "command substitution strips every trailing newline and a sentinel keeps them" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'x=$(printf "a\n\n\n"); printf "[%s]\n" "$x"
y=$(printf "a\n\n"; echo .); y=${y%.}; printf "[%s]\n" "$y"'
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf '[a]\n[a\n\n]')" ]
}

@test "text that shlex.quote produced evals back to exactly one word, in every shell" {
  # shlex.quote("it's $HOME; touch pwned `x`") returns this single-quote form,
  # with each embedded quote written as '"'"' (Lib/shlex.py, quote).
  q="'it'\"'\"'s \$HOME; touch pwned \`x\`'"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'cd "$2" || exit 1; eval "set -- $1"; printf "%s\n" "$#" "$1"; [ ! -e pwned ]' \
    sh "$q" "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # the expected text is literal on purpose
  [ "$output" = "$(printf '1\nit'\''s $HOME; touch pwned `x`')" ]
}

@test "POSIX leftmost-longest: every awk takes the longer alternative that Python re would skip" {
  awks=$(lab_awks)
  [ -n "$awks" ]
  while IFS= read -r awk_cmd; do
    read -r -a awk_words <<<"$awk_cmd"
    # shellcheck disable=SC2016 # the expansions belong to the shell under test
    run_lab_c '"$@" "BEGIN { match(\"abcd\", /ab|abcd/); print RSTART, RLENGTH }"' \
      sh "${awk_words[@]}" </dev/null
    [ "$status" -eq 0 ]
    [ "$output" = "1 4" ]
  done <<<"$awks"
}

@test "csvcol.sh hands python3 the program byte for byte on fd 3 and each value as one argument" {
  [ -n "$CSVCOL_BODY" ]
  run_lab "$CSVCOL" 'my col' 'a b.csv' </dev/null
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "argv: <-I> </dev/fd/3> <my col> <a b.csv>" ]
  [ "$output" = "$(printf 'argv: <-I> </dev/fd/3> <my col> <a b.csv>\nprogram:\n%s\nstdin:' "$CSVCOL_BODY")" ]
}

@test "csvcol.sh leaves standard input to the CSV data" {
  lab_shell_words
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'printf "h\n1\n" | "$@"' sh "${LAB_SHELL_WORDS[@]}" "$CSVCOL" h
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf 'argv: <-I> </dev/fd/3> <h>\nprogram:\n%s\nstdin:\nh\n1' "$CSVCOL_BODY")" ]
}

@test "csvcol.sh with no column exits 2 with a usage line and never starts python3" {
  run_lab "$CSVCOL"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: csvcol.sh COLUMN [FILE...]" ]
}

@test "pyshim rejects an unknown mode with status 2" {
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c '"$1" bogus' sh "$SHIM"
  [ "$status" -eq 2 ]
  [ "$output" = "usage: pyshim ignpipe CMD [ARG...] | sigstate | pipe | exit N" ]
}
