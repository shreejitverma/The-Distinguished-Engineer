#!/usr/bin/env bats
# Tests for indexed arrays: bash semantics, version changes, and how ksh93, mksh, posh, zsh and yash differ.
# POSIX sh has no arrays, so dash and BusyBox ash must reject the syntax; each test asserts that too.
# shellcheck disable=SC2016,SC2030,SC2031 # code strings and payloads belong to the shell under test; HIT is exported per test on purpose

setup() {
  load ../lib/lab
  FAMILY=$(lab_shell_family)
  VERSION=$(lab_shell_version)
  ARGV="$BATS_TEST_DIRNAME/argv-array.sh"
  GUARD="$BATS_TEST_DIRNAME/index-guard.sh"
}

# bash_at_least MAJOR MINOR: the bash under test (VERSION like 5.2.37(1)-release) is MAJOR.MINOR or newer.
bash_at_least() {
  local major minor rest
  major=${VERSION%%.*}
  rest=${VERSION#*.}
  minor=${rest%%[!0-9]*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# bash_posix_mode: the bash under test runs in POSIX mode (as macOS /bin/sh does); leaves status alone.
bash_posix_mode() {
  lab_shell_words
  "${LAB_SHELL_WORDS[@]}" -c '[ -o posix ]' 2>/dev/null
}

# ksh93_is_m: the ksh93 under test is ksh93u+m (hardened subscripts), not ksh93u+ 2012.
ksh93_is_m() {
  case $VERSION in
    *93u+m*) return 0 ;;
    *) return 1 ;;
  esac
}

# expect_no_compound: the shell under test has no name=(...) syntax and reported a syntax error.
expect_no_compound() {
  case $FAMILY in
    dash)
      [ "$status" -eq 2 ]
      [[ $output == *'Syntax error: "(" unexpected'* ]]
      ;;
    busybox)
      [ "$status" -eq 2 ]
      [[ $output == *'syntax error: unexpected "("'* ]]
      ;;
    posh)
      [ "$status" -eq 1 ]
      [[ $output == *"syntax error: \`(' unexpected"* ]]
      ;;
    *) false ;;
  esac
}

# expect_not_found WORD: dash or BusyBox ash ran an element assignment WORD as a command name.
expect_not_found() {
  [ "$status" -eq 2 ]
  [[ $output == *"$1: not found"* ]]
}

# empty_array_setup: code that leaves the variable a an indexed array with no elements in this shell.
# zsh and yash cannot unset one element (and native zsh rejects index 0); ksh93 reads a=() as a
# compound variable, so it and posh (no a=() at all) empty the array by unsetting its only element.
empty_array_setup() {
  case $FAMILY in
    zsh | zsh-sh | yash) printf '%s\n' 'a=()' ;;
    *) printf '%s\n' "a[0]=x; unset 'a[0]'" ;;
  esac
}

# --- creating and reading ---

@test "a compound assignment numbers elements from zero, but from one in native zsh and yash" {
  run_lab_c 'a=(x y z); echo "${a[0]}|${a[1]}|${#a[@]}"'
  case $FAMILY in
    bash | ksh93 | mksh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = 'x|y|3' ]
      ;;
    zsh)
      [ "$status" -eq 0 ]
      [ "$output" = '|x|3' ]
      ;;
    yash)
      # ${#a[@]} is the length of each element in yash; yash counts elements with ${a[#]}.
      [ "$status" -eq 0 ]
      [ "$output" = '|x|1 1 1' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "an array name without a subscript means element zero, but every element in native zsh and yash" {
  run_lab_c 'a=(x y z); echo "$a"'
  case $FAMILY in
    bash | ksh93 | mksh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = x ]
      ;;
    zsh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = 'x y z' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "an element needs braces: the unbraced form is element zero followed by a literal bracket, except in native zsh" {
  run_lab_c 'a=(x y); echo "$a[1]"'
  case $FAMILY in
    bash | ksh93 | mksh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = 'x[1]' ]
      ;;
    zsh)
      [ "$status" -eq 0 ]
      [ "$output" = x ]
      ;;
    yash)
      # $a is every element in yash, so the bracket text attaches to the last one.
      [ "$status" -eq 0 ]
      [ "$output" = 'x y[1]' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "a subscript is an arithmetic expression, and its side effects persist in the current shell" {
  run_lab_c 'i=0; a[i++]=x; a[i++]=y; echo "$i ${a[*]}"'
  case $FAMILY in
    bash | ksh93 | mksh | posh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = '2 x y' ]
      ;;
    zsh)
      # Native zsh numbers from 1, so the first assignment to index 0 is an error.
      [ "$status" -eq 1 ]
      [[ $output == *'assignment to invalid subscript range'* ]]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [[ $output == *"no such command \`a[i++]=x'"* ]]
      [ "${lines[2]% }" = 0 ]
      ;;
    dash | busybox) expect_not_found 'a[i++]=x' ;;
    *) false ;;
  esac
}

@test "a leading zero makes a subscript octal in bash, posh and zsh --emulate sh, where 08 is an error" {
  run_lab_c 'a[010]=x; echo "${a[8]-u}|${a[10]-u}"; d=08; echo "${a[d]-u}"; echo end'
  case $FAMILY in
    bash)
      [ "$status" -eq 1 ]
      [ "${lines[0]}" = 'x|u' ]
      [[ $output == *'08: value too great for base'* ]]
      [[ $output != *end* ]]
      ;;
    zsh-sh)
      [ "$status" -eq 1 ]
      [ "${lines[0]}" = 'x|u' ]
      [[ $output == *'bad math expression'* ]]
      ;;
    posh)
      [ "$status" -eq 1 ]
      [ "${lines[0]}" = 'x|u' ]
      [[ $output == *"bad number \`08'"* ]]
      ;;
    ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = $'u|x\nu\nend' ]
      ;;
    zsh)
      # Decimal, and index 10 of a one-based array: indices 1 to 9 are now set to the empty string.
      [ "$status" -eq 0 ]
      [ "$output" = $'|x\n\nend' ]
      ;;
    yash)
      [ "$status" -eq 2 ]
      [[ $output == *"no such command \`a[010]=x'"* ]]
      [[ $output == *'the index is not an integer'* ]]
      ;;
    dash | busybox) expect_not_found 'a[010]=x' ;;
    *) false ;;
  esac
}

@test "a negative subscript counts back from the end in bash 4.3 and later, ksh93 and zsh --emulate sh" {
  run_lab_c 'a[0]=x; a[1]=y; a[2]=z; a[-1]=Z; echo "${a[*]}"'
  case $FAMILY in
    bash)
      if bash_at_least 4 3; then
        [ "$status" -eq 0 ]
        [ "$output" = 'x y Z' ]
      else
        # The failed assignment ends the shell: status 1, or 127 in POSIX mode (macOS /bin/sh).
        if bash_posix_mode; then
          [ "$status" -eq 127 ]
        else
          [ "$status" -eq 1 ]
        fi
        [[ $output == *'a[-1]: bad array subscript'* ]]
        [[ $output != *'x y'* ]]
      fi
      ;;
    ksh93 | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = 'x y Z' ]
      ;;
    mksh)
      # mksh indices are unsigned 32-bit, so -1 is index 4294967295: a new fourth element.
      [ "$status" -eq 0 ]
      [ "$output" = 'x y z Z' ]
      run_lab_c 'a[-1]=Z; echo "${!a[@]}"'
      [ "$output" = 4294967295 ]
      ;;
    posh)
      [ "$status" -eq 1 ]
      [[ $output == *'subscript out of range'* ]]
      ;;
    zsh)
      [ "$status" -eq 1 ]
      [[ $output == *'assignment to invalid subscript range'* ]]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [[ $output == *"no such command \`a[-1]=Z'"* ]]
      ;;
    dash | busybox) expect_not_found 'a[-1]=Z' ;;
    *) false ;;
  esac
}

@test "the largest index differs: 2^63-1 in bash, 2^32-1 in mksh, 4194303 in ksh93, 1023 in posh" {
  case $FAMILY in
    bash)
      run_lab_c 'a[9223372036854775807]=x; echo "${!a[@]}"'
      [ "$status" -eq 0 ]
      [ "$output" = 9223372036854775807 ]
      # Appending past the maximum overflows the next index; bash 5.3 rejects it, older bash stores it.
      run_lab_c 'a[9223372036854775807]=x; a+=(y); echo "${!a[@]}"'
      if bash_at_least 5 3; then
        if bash_posix_mode; then
          [ "$status" -eq 127 ]
        else
          [ "$status" -eq 1 ]
        fi
        [[ $output == *'a[-9223372036854775808]: bad array subscript'* ]]
      else
        [ "$status" -eq 0 ]
        [ "$output" = '-9223372036854775808 9223372036854775807' ]
      fi
      ;;
    mksh)
      # 4294967296 wraps to index 0.
      run_lab_c 'a[4294967295]=x; a[4294967296]=y; echo "${!a[@]}"'
      [ "$status" -eq 0 ]
      [ "$output" = '0 4294967295' ]
      ;;
    ksh93)
      run_lab_c 'a[4194303]=x; echo "${!a[@]}"; a[4194304]=y; echo after'
      [ "$status" -eq 1 ]
      [ "${lines[0]}" = 4194303 ]
      [[ $output == *'subscript out of range'* ]]
      [[ $output != *after* ]]
      ;;
    posh)
      run_lab_c 'a[1023]=x; echo "${#a[@]}"; a[1024]=y; echo after'
      [ "$status" -eq 1 ]
      [ "${lines[0]}" = 1 ]
      [[ $output == *'subscript out of range'* ]]
      [[ $output != *after* ]]
      ;;
    zsh | zsh-sh)
      # zsh arrays are dense: one assignment allocates every element before it.
      run_lab_c 'a[100000]=x; echo "${#a[@]}"'
      [ "$status" -eq 0 ]
      if [ "$FAMILY" = zsh ]; then
        [ "$output" = 100000 ]
      else
        [ "$output" = 100001 ]
      fi
      ;;
    yash)
      # yash has no element assignment syntax; the array builtin (array -s) sets an element.
      run_lab_c 'a=(x); a[1]=y'
      [ "$status" -eq 127 ]
      [[ $output == *"no such command \`a[1]=y'"* ]]
      ;;
    dash | busybox)
      run_lab_c 'a[1]=x'
      [ "$status" -eq 127 ]
      [[ $output == *'a[1]=x: not found'* ]]
      ;;
    *) false ;;
  esac
}

# --- sparse arrays ---

@test "a count of set elements and the list of their indices show the gaps an assignment leaves" {
  run_lab_c 'a[0]=x; a[5]=f; echo "${#a[@]}"; echo "${!a[@]}"'
  case $FAMILY in
    bash | ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = $'2\n0 5' ]
      ;;
    posh)
      # posh has the count but not the ${!a[@]} list of indices.
      [ "$status" -eq 1 ]
      [ "${lines[0]}" = 2 ]
      [[ $output == *'bad substitution'* ]]
      ;;
    zsh-sh)
      # zsh fills indices 1 to 4 with empty strings, so the count is 6, and it has no ${!a[@]}.
      [ "$status" -eq 1 ]
      [ "${lines[0]}" = 6 ]
      [[ $output == *'bad substitution'* ]]
      ;;
    zsh)
      [ "$status" -eq 1 ]
      [[ $output == *'assignment to invalid subscript range'* ]]
      ;;
    yash)
      [ "$status" -eq 2 ]
      [[ $output == *'syntax error'* ]]
      ;;
    dash | busybox) expect_not_found 'a[0]=x' ;;
    *) false ;;
  esac
}

@test "unsetting one element leaves a gap, and copying through the all-elements expansion renumbers from zero" {
  run_lab_c 'a=(x y z); unset "a[1]"; b=("${a[@]}"); echo "${!a[@]}|${!b[@]}|${b[1]}"'
  case $FAMILY in
    bash | ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = '0 2|0 1|z' ]
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 1 ]
      [[ $output == *'bad substitution'* ]]
      ;;
    yash)
      [ "$status" -eq 2 ]
      [[ $output == *'syntax error'* ]]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "zsh arrays have no gaps: unset empties an element and an assignment past the end fills with empty strings" {
  run_lab_c 'a=(x y z); unset "a[2]"; echo "${#a[@]}"; a[6]=f; echo "${#a[@]}"'
  case $FAMILY in
    bash | ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = $'2\n3' ]
      ;;
    zsh)
      [ "$status" -eq 0 ]
      [ "$output" = $'3\n6' ]
      ;;
    zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = $'3\n7' ]
      ;;
    yash)
      # unset leaves all three elements (each of length 1), and a[6]=f is a command name.
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = '1 1 1' ]
      [[ $output == *"no such command \`a[6]=f'"* ]]
      [ "${lines[2]}" = '1 1 1' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "slicing a sparse array starts at the first set index at or after the offset in bash and ksh93" {
  run_lab_c 'a[1]=b; a[5]=f; a[9]=j; printf "[%s]" "${a[@]:2:2}"; echo'
  case $FAMILY in
    bash | ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = '[f][j]' ]
      ;;
    zsh | zsh-sh)
      # Dense arrays: the slice picks two of the empty strings that fill the gaps.
      [ "$status" -eq 0 ]
      [ "$output" = '[][]' ]
      ;;
    mksh | posh)
      [ "$status" -eq 1 ]
      [[ $output == *'bad substitution'* ]]
      ;;
    yash)
      [ "$status" -eq 2 ]
      [[ $output == *'syntax error'* ]]
      ;;
    dash | busybox) expect_not_found 'a[1]=b' ;;
    *) false ;;
  esac
}

# --- expanding ---

@test "the star expansion in quotes joins with the first IFS character, while the at expansion keeps one field per element" {
  run_lab_c 'a=("a b" c); IFS=,; echo "${a[*]}"; set -- "${a[@]}"; echo "$#"'
  case $FAMILY in
    bash | ksh93 | mksh | zsh | zsh-sh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = $'a b,c\n2' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "assigning the at expansion to a scalar joins with spaces in bash, ksh93 and mksh, but with IFS in zsh and yash" {
  run_lab_c 'a=(x y z); IFS=,; s="${a[@]}"; echo "$s"'
  case $FAMILY in
    bash | ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = 'x y z' ]
      ;;
    zsh | zsh-sh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = 'x,y,z' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "the quoted at expansion of a name never assigned is no field in bash, ksh93, mksh and posh, but one empty field in zsh and yash" {
  run_lab_c 'set -- "${nosuch[@]}"; echo "$#"'
  case $FAMILY in
    bash | ksh93 | mksh | posh)
      [ "$status" -eq 0 ]
      [ "$output" = 0 ]
      ;;
    zsh | zsh-sh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = 1 ]
      ;;
    dash)
      [ "$status" -eq 2 ]
      [[ $output == *'Bad substitution'* ]]
      ;;
    busybox)
      [ "$status" -eq 2 ]
      [[ $output == *'bad substitution'* ]]
      ;;
    *) false ;;
  esac
}

@test "under set -u an empty at expansion is an error in bash before 4.4, mksh and posh" {
  local setup
  setup=$(empty_array_setup)
  run_lab_c "set -u; $setup; echo \"[\${a[@]}]\"; echo ok"
  case $FAMILY in
    bash)
      if bash_at_least 4 4; then
        [ "$status" -eq 0 ]
        [ "$output" = $'[]\nok' ]
      else
        [ "$status" -eq 127 ]
        [[ $output == *'a[@]: unbound variable'* ]]
      fi
      ;;
    ksh93 | zsh | zsh-sh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = $'[]\nok' ]
      ;;
    mksh | posh)
      [ "$status" -eq 1 ]
      [[ $output == *'a[@]: parameter not set'* ]]
      ;;
    dash | busybox) expect_not_found 'a[0]=x' ;;
    *) false ;;
  esac
}

@test "printf runs its format once for an empty array, although the at expansion gives zero fields" {
  local setup
  setup=$(empty_array_setup)
  run_lab_c "$setup; printf '<%s>\n' \"\${a[@]}\"; set -- \"\${a[@]}\"; echo \"n=\$#\""
  case $FAMILY in
    bash | ksh93 | mksh | posh | zsh | zsh-sh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = $'<>\nn=0' ]
      ;;
    dash | busybox) expect_not_found 'a[0]=x' ;;
    *) false ;;
  esac
}

@test "ksh93 reads a=() as an empty compound variable, not an empty indexed array" {
  run_lab_c 'a=(); echo "${#a[@]}"'
  case $FAMILY in
    ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = 1 ]
      run_lab_c 'a=(); typeset -p a'
      [ "$output" = 'typeset -C a=()' ]
      ;;
    bash | mksh | zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = 0 ]
      ;;
    yash)
      # No elements, so the per-element lengths are an empty list.
      [ "$status" -eq 0 ]
      [ -z "$output" ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "plus-equals without parentheses appends to element zero's string, except in native zsh" {
  run_lab_c 'a=(x y); a+=z; echo "${a[*]}"'
  case $FAMILY in
    bash | ksh93 | mksh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = 'xz y' ]
      ;;
    zsh)
      [ "$status" -eq 0 ]
      [ "$output" = 'x y z' ]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [[ $output == *"no such command \`a+=z'"* ]]
      [ "${lines[1]}" = 'x y' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

# --- the environment, pipelines, and builtins ---

@test "export passes no array: bash and zsh export nothing, ksh93 and mksh element zero, yash the elements joined by colons" {
  run_lab_c 'a=(x y); export a; printenv a || echo unset'
  case $FAMILY in
    bash | zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = unset ]
      ;;
    ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = x ]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [ "$output" = 'x:y' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "appends inside a piped while loop are lost where the last pipeline stage runs in a subshell" {
  run_lab_c 'printf "x\ny\n" | while read -r l; do a+=("$l"); done; echo "n=${#a[@]}"'
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      [ "$output" = n=0 ]
      # lastpipe (bash 4.2+, job control off) runs the last stage in the current shell.
      run_lab_c 'shopt -s lastpipe; printf "x\ny\n" | while read -r l; do a+=("$l"); done; echo "n=${#a[@]}"'
      if bash_at_least 4 2; then
        [ "$output" = n=2 ]
      else
        [[ $output == *'lastpipe: invalid shell option name'* ]]
        [ "${lines[1]}" = n=0 ]
      fi
      ;;
    mksh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = n=0 ]
      ;;
    ksh93 | zsh)
      [ "$status" -eq 0 ]
      [ "$output" = n=2 ]
      ;;
    yash)
      # yash has no += for arrays.
      [ "$status" -eq 2 ]
      [[ $output == *'syntax error'* ]]
      ;;
    dash | busybox)
      [ "$status" -eq 2 ]
      [[ $output == *'yntax error'* ]]
      ;;
    posh)
      [ "$status" -eq 1 ]
      [[ $output == *'syntax error'* ]]
      ;;
    *) false ;;
  esac
}

@test "an unquoted a[0] given to unset is a glob, so a file named a0 changes what is unset" {
  run_lab_c 'cd "$1" || exit 9; a=(x y); unset a[0]; n1=${#a[@]}; : >a0; a=(x y); unset a[0]; echo "$n1 ${#a[@]}"' sh "$BATS_TEST_TMPDIR"
  case $FAMILY in
    bash | ksh93 | mksh)
      # Without the file: one element left. With it: unset a0 (another variable), both elements left.
      [ "$status" -eq 0 ]
      [ "$output" = '1 2' ]
      ;;
    zsh)
      # Native zsh reports the glob that matched nothing instead.
      [ "$status" -eq 1 ]
      [[ $output == *'no matches found: a[0]'* ]]
      ;;
    zsh-sh)
      # zsh never removes an element (it empties it), so the count stays 2 either way.
      [ "$status" -eq 0 ]
      [ "$output" = '2 2' ]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [ "$output" = '1 1 1 1' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "read -a fills words in bash and ksh93u+m but character codes in mksh, and ksh93, mksh, zsh and yash take read -A" {
  run_lab_c 'read -r -a a <<EOF
p q r
EOF
echo "${a[2]}"'
  local lower_a=$output lower_status=$status
  run_lab_c 'read -r -A a <<EOF
p q r
EOF
echo "${a[2]}"'
  case $FAMILY in
    bash)
      [ "$lower_a" = r ]
      [[ $output == *'read: -A: invalid option'* ]]
      ;;
    ksh93)
      if ksh93_is_m; then
        [ "$lower_a" = r ]
      else
        [[ $lower_a == *'read: -a: unknown option'* ]]
      fi
      [ "$output" = r ]
      ;;
    mksh)
      # mksh read -a stores the code of each character: a[2] is "q", 113.
      [ "$lower_a" = 113 ]
      [ "$output" = r ]
      ;;
    zsh)
      [[ $lower_a == *'bad option: -a'* ]]
      [ "$output" = q ]
      ;;
    zsh-sh)
      [[ $lower_a == *'bad option: -a'* ]]
      [ "$output" = r ]
      ;;
    yash)
      [[ $lower_a == *"\`-a' is not a valid option"* ]]
      [ "$output" = q ]
      ;;
    posh)
      [[ $lower_a == *"invalid option -- 'a'"* ]]
      [[ $output == *"invalid option -- 'A'"* ]]
      ;;
    dash)
      [ "$lower_status" -eq 2 ]
      [[ $lower_a == *'Illegal option -a'* ]]
      [[ $output == *'Illegal option -A'* ]]
      ;;
    busybox)
      [ "$lower_status" -eq 2 ]
      [[ $lower_a == *'illegal option -a'* ]]
      [[ $output == *'illegal option -A'* ]]
      ;;
    *) false ;;
  esac
}

@test "typeset -p prints a definition that recreates the array, and bash 4.4 dropped its outer quotes" {
  run_lab_c 'a=(x "y z"); typeset -p a'
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      if bash_at_least 4 4; then
        [ "$output" = 'declare -a a=([0]="x" [1]="y z")' ]
      else
        [ "$output" = "declare -a a='([0]=\"x\" [1]=\"y z\")'" ]
      fi
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = "typeset -a a=(x 'y z')" ]
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "typeset -a a=( x 'y z' )" ]
      ;;
    mksh)
      [ "$status" -eq 0 ]
      [ "$output" = $'set -A a\ntypeset a[0]=x\ntypeset a[1]=\'y z\'' ]
      ;;
    yash)
      [ "$status" -eq 0 ]
      [ "$output" = $'a=(x \'y z\')\ntypeset a' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "unset of the at subscript empties an indexed array in bash 5.2 and later, but removed the variable before" {
  run_lab_c 'a=(x y); unset "a[@]"; typeset -p a; echo "st=$?"'
  case $FAMILY in
    bash)
      if bash_at_least 5 2; then
        [ "$output" = $'declare -a a=()\nst=0' ]
      else
        [[ $output == *'a: not found'* ]]
        [ "${lines[1]}" = st=1 ]
      fi
      ;;
    ksh93)
      [[ $output == *'arithmetic syntax error'* ]]
      [ "${lines[1]}" = 'typeset -a a=(x y)' ]
      ;;
    zsh | zsh-sh)
      [ "$output" = $'typeset -a a=( \'\' )\nst=0' ]
      ;;
    mksh)
      [ "$status" -eq 1 ]
      [[ $output == *"unexpected '@'"* ]]
      ;;
    yash)
      [ "$output" = $'a=(x y)\ntypeset a\nst=0' ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

# --- untrusted indices ---

@test "a command hidden in an index value runs when the element is read through the dollar subscript" {
  export HIT="$BATS_TEST_TMPDIR/hit"
  run_lab "$GUARD" raw 'x[$(: >"$HIT")]'
  case $FAMILY in
    bash | mksh)
      [ "$status" -eq 0 ]
      [ -e "$HIT" ]
      ;;
    ksh93)
      if ksh93_is_m; then
        [ "$status" -eq 1 ]
        [ ! -e "$HIT" ]
      else
        [ -e "$HIT" ]
      fi
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ ! -e "$HIT" ]
      ;;
    yash)
      [ "$status" -eq 2 ]
      [ ! -e "$HIT" ]
      ;;
    dash | busybox | posh)
      expect_no_compound
      [ ! -e "$HIT" ]
      ;;
    *) false ;;
  esac
}

@test "the hidden command also runs on assignment, read, and unset with an untrusted index" {
  export HIT_A="$BATS_TEST_TMPDIR/assign" HIT_R="$BATS_TEST_TMPDIR/read" HIT_U="$BATS_TEST_TMPDIR/unset"
  run_lab_c 'a[0]=q; a[$1]=v; read -r "a[$2]" <<EOF
v
EOF
unset "a[$3]"; echo done' sh 'x[$(: >"$HIT_A")]' 'x[$(: >"$HIT_R")]' 'x[$(: >"$HIT_U")]'
  local hits=''
  [ ! -e "$HIT_A" ] || hits="${hits}A"
  [ ! -e "$HIT_R" ] || hits="${hits}R"
  [ ! -e "$HIT_U" ] || hits="${hits}U"
  case $FAMILY in
    bash | mksh) [ "$hits" = ARU ] ;;
    ksh93)
      if ksh93_is_m; then
        [ -z "$hits" ]
      else
        [ "$hits" = ARU ]
      fi
      ;;
    zsh-sh) [ "$hits" = RU ] ;;
    posh)
      # posh runs the assignment's hidden command, then fails to parse the empty result.
      [ "$status" -eq 1 ]
      [ "$hits" = A ]
      ;;
    zsh)
      # Native zsh stops at a[0]=q (index 0 is invalid there) before any payload.
      [ "$status" -eq 1 ]
      [ -z "$hits" ]
      ;;
    yash | dash | busybox) [ -z "$hits" ] ;;
    *) false ;;
  esac
}

@test "bash 5.2 stopped re-expanding a dollar subscript inside arithmetic, but a bare name and the dollar subscript still run the hidden command" {
  export HIT1="$BATS_TEST_TMPDIR/bare" HIT2="$BATS_TEST_TMPDIR/word" HIT3="$BATS_TEST_TMPDIR/arith"
  run_lab_c 'a=(q); i=$1; : $(( a[i] )); : "${a[$2]}"; : $(( a[$3] )); echo done' sh 'x[$(: >"$HIT1")]' 'x[$(: >"$HIT2")]' 'x[$(: >"$HIT3")]'
  local hits=''
  [ ! -e "$HIT1" ] || hits="${hits}1"
  [ ! -e "$HIT2" ] || hits="${hits}2"
  [ ! -e "$HIT3" ] || hits="${hits}3"
  case $FAMILY in
    bash)
      if bash_at_least 5 2; then
        [ "$status" -eq 1 ]
        [ "$hits" = 12 ]
      else
        [ "$status" -eq 0 ]
        [ "$hits" = 123 ]
      fi
      ;;
    mksh)
      [ "$status" -eq 0 ]
      [ "$hits" = 123 ]
      ;;
    ksh93)
      [ "$status" -eq 1 ]
      if ksh93_is_m; then
        [ -z "$hits" ]
      else
        # ksh93u+ 2012 runs the first payload, then aborts on the unset x.
        [ "$hits" = 1 ]
      fi
      ;;
    zsh | zsh-sh)
      # zsh runs only the payload pasted into an arithmetic expansion as text.
      [ "$status" -eq 0 ]
      [ "$hits" = 3 ]
      ;;
    yash)
      [ "$status" -eq 2 ]
      [ -z "$hits" ]
      ;;
    dash | busybox | posh)
      expect_no_compound
      [ -z "$hits" ]
      ;;
    *) false ;;
  esac
}

@test "the guard rejects a non-decimal, leading-zero, too-long or out-of-range index before evaluating it" {
  export HIT="$BATS_TEST_TMPDIR/hit"
  case $FAMILY in
    bash | ksh93 | mksh | zsh | zsh-sh | yash)
      for idx in 'x[$(: >"$HIT")]' 01 3 4294967296 '' -1; do
        run_lab "$GUARD" guarded "$idx"
        [ "$status" -eq 2 ]
        [ -z "$output" ]
      done
      [ ! -e "$HIT" ]
      ;;
    dash | busybox | posh)
      run_lab "$GUARD" guarded 'x[$(: >"$HIT")]'
      expect_no_compound
      [ ! -e "$HIT" ]
      ;;
    *) false ;;
  esac
}

@test "an index that passes the guard selects element 1, which is the first element in native zsh and yash" {
  run_lab "$GUARD" guarded 1
  case $FAMILY in
    bash | ksh93 | mksh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = green ]
      ;;
    zsh | yash)
      [ "$status" -eq 0 ]
      [ "$output" = red ]
      # The zero-based guard admits 0, which is no element at all in a one-based array.
      run_lab "$GUARD" guarded 0
      [ "$status" -eq 0 ]
      [ -z "$output" ]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}

@test "index-guard.sh rejects a wrong operand count or an unknown mode with status 64" {
  case $FAMILY in
    bash | ksh93 | mksh | zsh | zsh-sh | yash)
      run_lab "$GUARD" guarded
      [ "$status" -eq 64 ]
      run_lab "$GUARD" other 1
      [ "$status" -eq 64 ]
      [ -z "$output" ]
      ;;
    dash | busybox | posh)
      run_lab "$GUARD" other 1
      expect_no_compound
      ;;
    *) false ;;
  esac
}

# --- the canonical use ---

@test "argv-array.sh builds a command line whose arguments keep their spaces, glob characters and emptiness" {
  run_lab "$ARGV" 'my name' 'a b.txt' '*' ''
  case $FAMILY in
    bash | ksh93 | mksh | zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = $'<--name=my name><a b.txt><*><>\n6 words' ]
      ;;
    yash)
      # yash has no += for arrays, so the script fails to parse.
      [ "$status" -eq 2 ]
      [[ $output == *'syntax error'* ]]
      ;;
    dash | busybox | posh) expect_no_compound ;;
    *) false ;;
  esac
}
