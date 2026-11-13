#!/usr/bin/env bats
# Extended globbing: the ksh operators ?( ) *( ) +( ) @( ) !( ), which shells have them and
# how they are switched on, where bash reads the switch, and the failure modes they bring.

setup() {
  load ../lib/lab
  LAB_DIR=$BATS_TEST_DIRNAME
}

# The truth table extmatch.sh prints wherever the operators exist (one line per pair).
TABLE_PAIRS=(
  '?(ab):' '?(ab):ab' '?(ab):abab' '*(ab):' '*(ab):ababab' '+(ab):' '+(ab):abab'
  '@(ab|cd):cd' '@(ab|cd):abcd' '!(ab|cd):ab' '!(ab|cd):abc' '!(ab|cd):' '!(foo)*:foo'
  '*.!(c):a.c' '*.!(c):a.cc' '+([0-9]).+([0-9]):10.25' '!(?)*:a' '!(*):x' '@(|a)b:b'
)
TABLE_EXPECTED='?(ab): 1
?(ab):ab 1
?(ab):abab 0
*(ab): 1
*(ab):ababab 1
+(ab): 0
+(ab):abab 1
@(ab|cd):cd 1
@(ab|cd):abcd 0
!(ab|cd):ab 0
!(ab|cd):abc 1
!(ab|cd): 1
!(foo)*:foo 1
*.!(c):a.c 0
*.!(c):a.cc 1
+([0-9]).+([0-9]):10.25 1
!(?)*:a 1
!(*):x 0
@(|a)b:b 1'

# bash_version_number: the bash under test as major * 100 + minor (5.3 -> 503).
bash_version_number() {
  local v major rest minor
  v=$(lab_shell_version)
  major=${v%%.*}
  rest=${v#*.}
  minor=${rest%%.*}
  printf '%d\n' "$((major * 100 + minor))"
}

# assert_no_shopt: the shell under test has no shopt builtin, so the bash-only switch is absent.
assert_no_shopt() {
  run_lab_c 'shopt -s extglob 2>/dev/null || exit 9'
  [ "$status" -eq 9 ]
}

@test "the five operators give the ksh truth table wherever the shell has them" {
  run_lab "$LAB_DIR/extmatch.sh" "${TABLE_PAIRS[@]}"
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=shopt
$TABLE_EXPECTED" ]
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=setopt
$TABLE_EXPECTED" ]
      ;;
    ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=native
$TABLE_EXPECTED" ]
      ;;
    dash | posh | busybox | yash)
      # @( in a case pattern is a syntax error in these parsers, so the probe falls back.
      [ "$status" -eq 3 ]
      [ "$output" = "mode=none" ]
      ;;
    *) false ;;
  esac
}

@test "pathname expansion: !( ) excludes, dot files stay hidden, and native zsh reads (c) as a qualifier" {
  dir=$BATS_TEST_TMPDIR/files
  mkdir "$dir"
  for f in a.c b.c a.h readme .hidden 1.log 12.log x.log notes.txt; do
    : >"$dir/$f"
  done
  run_lab "$LAB_DIR/extglob-files.sh" "$dir"
  common='1.log 12.log notes.txt readme x.log
a.c b.c
1.log 12.log'
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=shopt
$common
1.log 12.log a.h notes.txt x.log" ]
      ;;
    zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=setopt
$common
1.log 12.log a.h notes.txt x.log" ]
      ;;
    ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=native
$common
1.log 12.log a.h notes.txt x.log" ]
      ;;
    zsh)
      # BARE_GLOB_QUAL: a trailing (c) with no | is a glob qualifier list, and the c qualifier
      # (inode change time) needs a number, so the last pattern is an error, not a ksh pattern.
      [ "$status" -eq 1 ]
      [ "$output" = "mode=setopt
$common
(eval):1: number expected" ]
      ;;
    dash | posh | busybox | yash)
      [ "$status" -eq 3 ]
      [ "$output" = "mode=none" ]
      ;;
    *) false ;;
  esac
}

@test "find -name passes its pattern to fnmatch without extended matching, so @( ) is literal" {
  dir=$BATS_TEST_TMPDIR/find
  mkdir "$dir"
  : >"$dir/a.c"
  : >"$dir/b.c"
  : >"$dir/@(a|b).c"
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'find "$1" -name "@(a|b).c"; find "$1" -name "!(*.c)" | wc -l | tr -d " "' _ "$dir"
  [ "$status" -eq 0 ]
  [ "$output" = "$dir/@(a|b).c
0" ]
}

@test "!(true) is a negated subshell in dash and default bash but a pattern that runs a file in ksh, mksh, and bash with extglob" {
  dir=$BATS_TEST_TMPDIR/neg
  mkdir "$dir"
  : >"$dir/a.c"
  : >"$dir/b.c"
  run_lab "$LAB_DIR/extglob-probe.sh" negation "$dir"
  plain_status=$status plain_output=$output
  run_lab "$LAB_DIR/extglob-probe.sh" negation-extglob "$dir"
  ext_status=$status ext_output=$output
  [ "$plain_status" -eq 0 ]
  [ "$ext_status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "$plain_output" = "status=1" ]
      # With extglob on, !(true) expands to "a.c b.c" and bash tries to run a.c.
      [[ $ext_output == *"a.c: command not found"* ]]
      [[ $ext_output == *"status=127" ]]
      ;;
    ksh93 | mksh)
      [[ $plain_output == *"a.c"*"not found"* ]]
      [[ $plain_output == *"status=127" ]]
      [[ $ext_output == *"a.c"*"not found"* ]]
      [[ $ext_output == *"status=127" ]]
      ;;
    zsh)
      # Native zsh parses the trailing (true) as glob qualifiers t, r, u, e; u needs a delimiter.
      [[ $plain_output == *"missing delimiter for 'u' glob qualifier"* ]]
      [[ $plain_output == *"status=1" ]]
      [ "$ext_output" = "$plain_output" ]
      ;;
    zsh-sh | dash | posh | busybox | yash)
      [ "$plain_output" = "status=1" ]
      [ "$ext_output" = "status=1" ]
      ;;
    *) false ;;
  esac
}

@test "trimming with extended patterns works in ksh-family shells and silently does nothing elsewhere" {
  run_lab "$LAB_DIR/extglob-probe.sh" params
  [ "$status" -eq 0 ]
  extended='<a  b>
/a/b
x.tar x.tar .gz'
  plain='<  a  b  >
/a/b///
x.tar.gz x.tar.gz x.tar.gz'
  case $(lab_shell_family) in
    bash) [ "$output" = "mode=shopt
$extended" ] ;;
    zsh) [ "$output" = "mode=setopt
$extended" ] ;;
    ksh93 | mksh) [ "$output" = "mode=native
$extended" ] ;;
    zsh-sh)
      # KSH_GLOB is on and case patterns work, but sh emulation keeps parameter-expansion
      # patterns plain: no error, no trimming.
      [ "$output" = "mode=setopt
$plain" ]
      ;;
    dash | posh | busybox | yash) [ "$output" = "mode=none
$plain" ] ;;
    *) false ;;
  esac
}

@test "\${v//+( )/ } squeezes blanks where extended patterns exist and is a syntax error in dash and posh" {
  run_lab "$LAB_DIR/extglob-probe.sh" squeeze
  case $(lab_shell_family) in
    bash)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=shopt
<a b c>
<X   b    c>" ]
      ;;
    zsh)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=setopt
<a b c>
<X   b    c>" ]
      ;;
    ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=native
<a b c>
<X   b    c>" ]
      ;;
    zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "mode=setopt
<a   b    c>
<a   b    c>" ]
      ;;
    busybox | yash)
      # ${v//p/r} exists here, but +( ) is an ordinary pattern that matches nothing.
      [ "$status" -eq 0 ]
      [ "$output" = "mode=none
<a   b    c>
<a   b    c>" ]
      ;;
    dash)
      [ "$status" -eq 2 ]
      [[ $output == "mode=none"*"Bad substitution"* ]]
      ;;
    posh)
      [ "$status" -eq 1 ]
      [[ $output == "mode=none"*"bad substitution"* ]]
      ;;
    *) false ;;
  esac
}

@test "nested star patterns backtrack exponentially in bash and ksh93 but not in zsh" {
  run_lab "$LAB_DIR/extglob-probe.sh" backtrack 40
  case $(lab_shell_family) in
    bash | ksh93 | mksh)
      [ "$status" -eq 0 ]
      [[ $output == *"control=1 "* ]]
      [[ $output == *"result=nomatch"* ]]
      # One failed match of fewer than 31 bytes already took 2 clock seconds.
      len=${output##*len=}
      len=${len%% *}
      secs=${output##*secs=}
      [ "$len" -le 30 ]
      [ "$secs" -ge 2 ]
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [[ $output == *"control=1 len=40 result=nomatch secs="[01] ]]
      ;;
    dash | posh | busybox | yash)
      [ "$status" -eq 3 ]
      [ "$output" = "mode=none" ]
      ;;
    *) false ;;
  esac
}

@test "a star pattern recurses once per repetition and overflows an 8 MiB stack" {
  run_lab "$LAB_DIR/extglob-probe.sh" deep 2000
  small_status=$status small_output=$output
  run_lab "$LAB_DIR/extglob-probe.sh" deep 300000
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | ksh93 | mksh)
      [ "$small_status" -eq 0 ]
      [[ $small_output == *"len=2000 result=match" ]]
      # 128 + SIGSEGV (11): the shell itself died of stack exhaustion, printing no result.
      [ "$status" -eq 139 ]
      [[ $output != *"result="* ]]
      ;;
    dash | posh | busybox | yash)
      [ "$small_status" -eq 3 ]
      [ "$status" -eq 3 ]
      [ "$output" = "mode=none" ]
      ;;
    *) false ;;
  esac
}

@test "bash reads extglob when it parses, so shopt on the same line is too late" {
  case $(lab_shell_family) in
    bash)
      run_lab_c 'shopt -s extglob; case a in @(a|b)) echo yes ;; esac'
      [ "$status" -eq 2 ]
      [[ $output == *"syntax error near unexpected token"* ]]
      run_lab_c 'shopt -s extglob
case a in @(a|b)) echo yes ;; esac'
      [ "$status" -eq 0 ]
      [ "$output" = yes ]
      ;;
    zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_no_shopt ;;
    *) false ;;
  esac
}

@test "bash also reads extglob when it matches: a function parsed with it on fails with it off" {
  case $(lab_shell_family) in
    bash)
      # shellcheck disable=SC2016 # the expansion belongs to the shell under test
      run_lab_c 'shopt -s extglob
f() { case $1 in @(a|b)) echo yes ;; *) echo no ;; esac; }
f a
shopt -u extglob
f a'
      [ "$status" -eq 0 ]
      [ "$output" = "yes
no" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_no_shopt ;;
    *) false ;;
  esac
}

@test "bash 5.2 parses command substitutions at definition time; BASH_COMPAT=51 restores the deferral" {
  case $(lab_shell_family) in
    bash)
      dir=$BATS_TEST_TMPDIR/comsub
      mkdir "$dir"
      : >"$dir/a.c"
      : >"$dir/b.c"
      # The definition runs in a subshell because bash 5.3 exits outright (status 127) on a
      # syntax error inside a command substitution, even under eval.
      # shellcheck disable=SC2016 # the expansions belong to the shell under test
      run_lab_c 'cd "$1" || exit
shopt -u extglob
(
  eval '\''f() { echo $(echo @(a|b).c); }'\''
  echo defined
  shopt -s extglob
  f
) 2>/dev/null
echo "status=$?"' _ "$dir"
      [ "$status" -eq 0 ]
      version=$(bash_version_number)
      if [ "$version" -ge 502 ]; then
        [[ $output =~ ^status=[1-9][0-9]*$ ]]
        # shellcheck disable=SC2016 # the expansions belong to the shell under test
        run_lab_c 'cd "$1" || exit
BASH_COMPAT=51
shopt -u extglob
(
  eval '\''f() { echo $(echo @(a|b).c); }'\''
  echo defined
  shopt -s extglob
  f
) 2>/dev/null
echo "status=$?"' _ "$dir"
        [ "$status" -eq 0 ]
      fi
      [ "$output" = "defined
a.c b.c
status=0" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_no_shopt ;;
    *) false ;;
  esac
}

@test "[[ == ]] forces extended patterns in bash: when parsing from 4.1, when matching in every version" {
  case $(lab_shell_family) in
    bash)
      version=$(bash_version_number)
      run_lab_c 'shopt extglob; [[ a == @(a|b) ]] && echo forced'
      if [ "$version" -ge 401 ]; then
        [ "$status" -eq 0 ]
        [[ $output == *"off"*"forced" ]]
      else
        [ "$status" -eq 2 ]
        [[ $output == *"syntax error"* ]]
      fi
      # shellcheck disable=SC2016 # the expansions belong to the shell under test
      run_lab_c 'p="@(a|b)"; [[ a == $p ]] && echo runtime; case a in $p) echo case ;; *) echo case-plain ;; esac'
      [ "$status" -eq 0 ]
      [ "$output" = "runtime
case-plain" ]
      ;;
    zsh | zsh-sh | ksh93 | mksh | dash | posh | busybox | yash) assert_no_shopt ;;
    *) false ;;
  esac
}
