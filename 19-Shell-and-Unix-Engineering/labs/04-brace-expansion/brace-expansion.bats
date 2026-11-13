#!/usr/bin/env bats
# Tests for brace-probe.sh: brace expansion across the shell matrix.
# Every test asserts each family's exact output; bash and ksh93 branch on their release
# where releases disagree, and an unexpected family or release fails.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/brace-probe.sh"
}

# bash_era: print 3.2, 4.4-5.2, 5.3, or unknown for the bash under test.
bash_era() {
  case $(lab_shell_version) in
    3.*) echo 3.2 ;;
    4.* | 5.[012].*) echo 4.4-5.2 ;;
    5.3.*) echo 5.3 ;;
    *) echo unknown ;;
  esac
}

# ksh_era: print u+m (ksh93u+m) or 2012 (ksh93u+ 2012-08-01) for the ksh93 under test.
ksh_era() {
  case $(lab_shell_version) in
    *93u+m*) echo u+m ;;
    *"93u+ 2012"*) echo 2012 ;;
    *) echo unknown ;;
  esac
}

@test "a comma list joins the preamble and postscript to each item, in source order, without touching the filesystem" {
  run_lab "$PROBE" list
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | ksh93 | mksh)
      [ "${lines[0]}" = "2 args: <abd> <acd>" ]
      [ "${lines[1]}" = "4 args: <a1> <a2> <b1> <b2>" ]
      [ "${lines[2]}" = "3 args: <abe> <ace> <ade>" ]
      [ "${lines[3]}" = "3 args: <c> <a> <b>" ]
      [ "${lines[4]}" = "2 args: </nonexistent-brace-lab/x> </nonexistent-brace-lab/y>" ]
      ;;
    dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "1 args: <a{b,c}d>" ]
      [ "${lines[1]}" = "1 args: <{a,b}{1,2}>" ]
      [ "${lines[2]}" = "1 args: <a{b,{c,d}}e>" ]
      [ "${lines[3]}" = "1 args: <{c,a,b}>" ]
      [ "${lines[4]}" = "1 args: </nonexistent-brace-lab/{x,y}>" ]
      ;;
    *) false ;;
  esac
}

@test "a sequence expression counts up, down, through letters, and through negative numbers" {
  run_lab "$PROBE" range
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | ksh93)
      [ "${lines[0]}" = "5 args: <1> <2> <3> <4> <5>" ]
      [ "${lines[1]}" = "5 args: <5> <4> <3> <2> <1>" ]
      [ "${lines[2]}" = "5 args: <a> <b> <c> <d> <e>" ]
      [ "${lines[3]}" = "5 args: <-2> <-1> <0> <1> <2>" ]
      ;;
    mksh | dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "1 args: <{1..5}>" ]
      [ "${lines[1]}" = "1 args: <{5..1}>" ]
      [ "${lines[2]}" = "1 args: <{a..e}>" ]
      [ "${lines[3]}" = "1 args: <{-2..2}>" ]
      ;;
    *) false ;;
  esac
}

@test "the step of a sequence: bash ignores its sign, zsh reverses on a negative step, ksh93 stops at the first term" {
  run_lab "$PROBE" step
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      case $(bash_era) in
        3.2)
          [ "${lines[0]}" = "1 args: <{1..10..3}>" ]
          [ "${lines[1]}" = "1 args: <{10..1..3}>" ]
          [ "${lines[2]}" = "1 args: <{1..10..-3}>" ]
          ;;
        4.4-5.2 | 5.3)
          [ "${lines[0]}" = "4 args: <1> <4> <7> <10>" ]
          [ "${lines[1]}" = "4 args: <10> <7> <4> <1>" ]
          [ "${lines[2]}" = "4 args: <1> <4> <7> <10>" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh)
      [ "${lines[0]}" = "4 args: <1> <4> <7> <10>" ]
      [ "${lines[1]}" = "4 args: <10> <7> <4> <1>" ]
      [ "${lines[2]}" = "4 args: <10> <7> <4> <1>" ]
      ;;
    ksh93)
      [ "${lines[0]}" = "4 args: <1> <4> <7> <10>" ]
      [ "${lines[1]}" = "1 args: <10>" ]
      [ "${lines[2]}" = "1 args: <1>" ]
      ;;
    mksh | dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "1 args: <{1..10..3}>" ]
      [ "${lines[1]}" = "1 args: <{10..1..3}>" ]
      [ "${lines[2]}" = "1 args: <{1..10..-3}>" ]
      ;;
    *) false ;;
  esac
}

@test "leading zeros pad in bash 4+ and zsh, are dropped by bash 3.2, and mean octal to ksh93" {
  run_lab "$PROBE" pad
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      case $(bash_era) in
        3.2)
          [ "${lines[0]}" = "3 args: <8> <9> <10>" ]
          [ "${lines[1]}" = "3 args: <10> <11> <12>" ]
          [ "${lines[2]}" = "3 args: <-1> <0> <1>" ]
          ;;
        4.4-5.2 | 5.3)
          [ "${lines[0]}" = "3 args: <08> <09> <10>" ]
          [ "${lines[1]}" = "3 args: <010> <011> <012>" ]
          [ "${lines[2]}" = "3 args: <-01> <000> <001>" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh)
      [ "${lines[0]}" = "3 args: <08> <09> <10>" ]
      [ "${lines[1]}" = "3 args: <010> <011> <012>" ]
      [ "${lines[2]}" = "3 args: <-01> <000> <001>" ]
      ;;
    ksh93)
      case $(ksh_era) in
        2012) [ "${lines[0]}" = "3 args: <8> <9> <10>" ] ;;
        u+m) [ "${lines[0]}" = "1 args: <{08..10}>" ] ;;
        *) false ;;
      esac
      [ "${lines[1]}" = "3 args: <8> <9> <10>" ]
      [ "${lines[2]}" = "3 args: <-1> <0> <1>" ]
      ;;
    mksh | dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "1 args: <{08..10}>" ]
      [ "${lines[1]}" = "1 args: <{010..012}>" ]
      [ "${lines[2]}" = "1 args: <{-01..1}>" ]
      ;;
    *) false ;;
  esac
}

@test "endpoints near 2^63 survive in 64-bit arithmetic and wrap in bash 3.2 and ksh93" {
  run_lab "$PROBE" width
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      case $(bash_era) in
        3.2) [ "$output" = "2 args: <-2> <-1>" ] ;;
        4.4-5.2 | 5.3) [ "$output" = "2 args: <9223372036854775806> <9223372036854775807>" ] ;;
        *) false ;;
      esac
      ;;
    zsh) [ "$output" = "2 args: <9223372036854775806> <9223372036854775807>" ] ;;
    ksh93) [ "$output" = "2 args: <-2> <-1>" ] ;;
    mksh | dash | posh | busybox | yash | zsh-sh)
      [ "$output" = "1 args: <{9223372036854775806..9223372036854775807}>" ]
      ;;
    *) false ;;
  esac
}

@test "bash expands braces before parameters and tildes; zsh, ksh93 and mksh expand them after" {
  run_lab "$PROBE" order
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "${lines[0]}" = "1 args: <{1..3}>" ]
      [ "${lines[1]}" = "2 args: <one> <two>" ]
      [ "${lines[2]}" = "2 args: <a1> <a2>" ]
      [ "${lines[3]}" = "2 args: <$HOME> <x>" ]
      ;;
    zsh)
      [ "${lines[0]}" = "3 args: <1> <2> <3>" ]
      [ "${lines[1]}" = "2 args: <a1> <a2>" ]
      [ "${lines[2]}" = "2 args: <a1> <a2>" ]
      [ "${lines[3]}" = "2 args: <$HOME> <x>" ]
      ;;
    ksh93)
      [ "${lines[0]}" = "3 args: <1> <2> <3>" ]
      [ "${lines[1]}" = "2 args: <a1> <a2>" ]
      [ "${lines[2]}" = "2 args: <a1> <a2>" ]
      [ "${lines[3]}" = "2 args: <~> <x>" ]
      ;;
    mksh)
      [ "${lines[0]}" = "1 args: <{1..3}>" ]
      [ "${lines[1]}" = "2 args: <a1> <a2>" ]
      [ "${lines[2]}" = "2 args: <a1> <a2>" ]
      [ "${lines[3]}" = "2 args: <~> <x>" ]
      ;;
    dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "1 args: <{1..3}>" ]
      [ "${lines[1]}" = "1 args: <a{1,2}>" ]
      [ "${lines[2]}" = "1 args: <a{1,2}>" ]
      [ "${lines[3]}" = "1 args: <{~,x}>" ]
      ;;
    *) false ;;
  esac
}

@test "braces in the value of an unquoted expansion are expanded only by ksh93 and mksh" {
  run_lab "$PROBE" from-expansion
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    ksh93 | mksh)
      [ "${lines[0]}" = "2 args: <a> <b>" ]
      [ "${lines[1]}" = "1 args: <{a,b}>" ]
      [ "${lines[2]}" = "2 args: <a> <b>" ]
      [ "${lines[3]}" = "2 args: <c> <d>" ]
      ;;
    bash | zsh | dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "1 args: <{a,b}>" ]
      [ "${lines[1]}" = "1 args: <{a,b}>" ]
      [ "${lines[2]}" = "1 args: <{a,b}>" ]
      [ "${lines[3]}" = "1 args: <{c,d}>" ]
      ;;
    *) false ;;
  esac
}

@test "quoting a brace or a comma disables it; quoting the text of an item does not" {
  run_lab "$PROBE" quoting
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "1 args: <{a,b}>" ]
  [ "${lines[1]}" = "1 args: <{a,b}>" ]
  [ "${lines[2]}" = "1 args: <{a,b}>" ]
  case $(lab_shell_family) in
    bash | zsh | ksh93 | mksh)
      [ "${lines[3]}" = "2 args: <a> <b,c>" ]
      [ "${lines[4]}" = "2 args: <a> <b c>" ]
      [ "${lines[5]}" = "2 args: <ab{d,e}> <ac{d,e}>" ]
      ;;
    dash | posh | busybox | yash | zsh-sh)
      [ "${lines[3]}" = "1 args: <{a,b,c}>" ]
      [ "${lines[4]}" = "1 args: <{a,b c}>" ]
      [ "${lines[5]}" = "1 args: <a{b,c}{d,e}>" ]
      ;;
    *) false ;;
  esac
}

@test "malformed braces stay literal, and bash alone drops an empty alternative that has no prefix or suffix" {
  run_lab "$PROBE" malformed
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "1 args: <{a}>" ]
  [ "${lines[1]}" = "1 args: <{}>" ]
  [ "${lines[2]}" = "1 args: <{a,b>" ]
  case $(lab_shell_family) in
    bash)
      [ "${lines[3]}" = "2 args: <xy> <xy>" ]
      [ "${lines[4]}" = "1 args: <a>" ]
      [ "${lines[5]}" = "0 args:" ]
      [ "${lines[6]}" = "2 args: <a{}> <b{}>" ]
      ;;
    zsh | ksh93 | mksh)
      [ "${lines[3]}" = "2 args: <xy> <xy>" ]
      [ "${lines[4]}" = "2 args: <a> <>" ]
      [ "${lines[5]}" = "2 args: <> <>" ]
      [ "${lines[6]}" = "2 args: <a{}> <b{}>" ]
      ;;
    dash | posh | busybox | yash | zsh-sh)
      [ "${lines[3]}" = "1 args: <x{,}y>" ]
      [ "${lines[4]}" = "1 args: <{a,}>" ]
      [ "${lines[5]}" = "1 args: <{,}>" ]
      [ "${lines[6]}" = "1 args: <{a,b}{}>" ]
      ;;
    *) false ;;
  esac
}

@test "an invalid sequence is literal text, and each bash release treats its neighbours differently" {
  run_lab "$PROBE" failed-sequence
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      case $(bash_era) in
        3.2)
          [ "${lines[0]}" = "1 args: <{1..x}{a,b}>" ]
          [ "${lines[1]}" = "2 args: <1..3> <2..3>" ]
          ;;
        4.4-5.2)
          [ "${lines[0]}" = "2 args: <{1..x}a> <{1..x}b>" ]
          [ "${lines[1]}" = "2 args: <1..3> <2..3>" ]
          ;;
        5.3)
          [ "${lines[0]}" = "2 args: <{1..x}a> <{1..x}b>" ]
          [ "${lines[1]}" = "2 args: <{1..3}> <{2..3}>" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh)
      # {1..x} is a character range from 1 (0x31) to x (0x78): 72 characters, times 2.
      case ${lines[0]} in
        "144 args: <1a> <1b> <2a> <2b> "*" <xa> <xb>") ;;
        *) false ;;
      esac
      [ "${lines[1]}" = "2 args: <{1..3}> <{2..3}>" ]
      ;;
    ksh93 | mksh)
      [ "${lines[0]}" = "2 args: <{1..x}a> <{1..x}b>" ]
      [ "${lines[1]}" = "1 args: <{{1,2}..3}>" ]
      ;;
    dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "1 args: <{1..x}{a,b}>" ]
      [ "${lines[1]}" = "1 args: <{{1,2}..3}>" ]
      ;;
    *) false ;;
  esac
}

@test "an assignment word keeps its braces, but bash brace expands an export argument into two assignments" {
  run_lab "$PROBE" assign
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "1 args: <{a,b}>" ]
  case $(lab_shell_family) in
    bash) [ "${lines[1]}" = "1 args: <b>" ] ;;
    zsh | ksh93 | mksh | dash | posh | busybox | yash | zsh-sh)
      [ "${lines[1]}" = "1 args: <{a,b}>" ]
      ;;
    *) false ;;
  esac
}

@test "bash copies a quoted dollar-at into every word a brace list generates; zsh, ksh93 and mksh expand only the last field" {
  run_lab "$PROBE" dollar-at
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash) [ "$output" = "4 args: <1> <2a> <1> <2b>" ] ;;
    zsh | ksh93 | mksh) [ "$output" = "3 args: <1> <2a> <2b>" ] ;;
    dash | posh | busybox | yash | zsh-sh) [ "$output" = "2 args: <1> <2{a,b}>" ] ;;
    *) false ;;
  esac
}

@test "a case pattern is never brace expanded" {
  run_lab "$PROBE" case-pattern
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | ksh93 | mksh | dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "a: no match" ]
      [ "${lines[1]}" = "{a,b}: matched" ]
      ;;
    *) false ;;
  esac
}

@test "a brace list as a redirection target: bash refuses it, zsh opens both files, the rest create a file named {a,b}" {
  dir=$(mktemp -d "$BATS_TEST_TMPDIR/redirect.XXXXXX")
  run_lab "$PROBE" redirect "$dir"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "$output" = "status=1" ]
      entries=$(ls -A "$dir")
      [ -z "$entries" ]
      ;;
    zsh)
      [ "${lines[0]}" = "status=0" ]
      [ "${lines[1]}" = "a" ]
      [ "${lines[2]}" = "b" ]
      [ "${#lines[@]}" -eq 3 ]
      ;;
    ksh93 | mksh | dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "status=0" ]
      [ "${lines[1]}" = "{a,b}" ]
      [ "${#lines[@]}" -eq 2 ]
      ;;
    *) false ;;
  esac
}

@test "each generated word is globbed on its own, so one unmatched word stays literal or, in zsh, aborts the command" {
  dir=$(mktemp -d "$BATS_TEST_TMPDIR/glob.XXXXXX")
  run_lab "$PROBE" glob "$dir"
  case $(lab_shell_family) in
    bash | ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "3 args: <b1> <b2> <a1>" ]
      [ "${lines[1]}" = "2 args: <f1.txt> <f*.nomatch>" ]
      ;;
    zsh)
      [ "$status" -ne 0 ]
      [ "${lines[0]}" = "3 args: <b1> <b2> <a1>" ]
      case ${lines[1]} in
        *"no matches found: f*.nomatch") ;;
        *) false ;;
      esac
      [ "${#lines[@]}" -eq 2 ]
      ;;
    dash | posh | busybox | yash | zsh-sh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "1 args: <{b,a}*>" ]
      [ "${lines[1]}" = "1 args: <f*.{txt,nomatch}>" ]
      ;;
    *) false ;;
  esac
}

@test "a brace list in command position builds a command with no blank in its source" {
  run_lab "$PROBE" command-position
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | ksh93 | mksh)
      [ "${lines[0]}" = "brace-ran" ]
      [ "${lines[1]}" = "status=0" ]
      ;;
    zsh | dash | posh | busybox | yash | zsh-sh)
      [ "$output" = "status=127" ]
      ;;
    *) false ;;
  esac
}

@test "the braceexpand option: bash, zsh and ksh93 support lists and both kinds of range, mksh lists only, yash lists and numbers" {
  run_lab "$PROBE" toggle
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | ksh93 | zsh-sh)
      [ "${lines[0]}" = "8 args: <a> <b> <1> <2> <3> <a> <b> <c>" ]
      [ "${lines[1]}" = "1 args: <{a,b}>" ]
      ;;
    mksh)
      [ "${lines[0]}" = "4 args: <a> <b> <{1..3}> <{a..c}>" ]
      [ "${lines[1]}" = "1 args: <{a,b}>" ]
      ;;
    yash)
      [ "${lines[0]}" = "6 args: <a> <b> <1> <2> <3> <{a..c}>" ]
      [ "${lines[1]}" = "1 args: <{a,b}>" ]
      ;;
    dash | posh | busybox)
      [ "${lines[0]}" = "no braceexpand option" ]
      [ "${lines[1]}" = "1 args: <{a,b}>" ]
      ;;
    *) false ;;
  esac
}

@test "set -f leaves brace expansion on, except in mksh, which brace expands only words it may glob" {
  run_lab "$PROBE" noglob
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | ksh93) [ "$output" = "2 args: <a> <b>" ] ;;
    mksh | dash | posh | busybox | yash | zsh-sh) [ "$output" = "1 args: <{a,b}>" ] ;;
    *) false ;;
  esac
}

@test "posix mode keeps brace expansion in bash and turns it off in ksh93u+m and mksh" {
  run_lab "$PROBE" posix-mode
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash) [ "$output" = "2 args: <a> <b>" ] ;;
    ksh93)
      case $(ksh_era) in
        u+m) [ "$output" = "1 args: <{a,b}>" ] ;;
        2012) [ "$output" = "no posix option" ] ;;
        *) false ;;
      esac
      ;;
    mksh | yash) [ "$output" = "1 args: <{a,b}>" ] ;;
    zsh | dash | posh | busybox | zsh-sh) [ "$output" = "no posix option" ] ;;
    *) false ;;
  esac
}

@test "declare -a arr=({1..3}): bash 3.2 keeps only the last element" {
  run_lab "$PROBE" array-declare
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      case $(bash_era) in
        3.2) [ "$output" = "$(printf '1 args: <3>\nstatus=0')" ] ;;
        4.4-5.2 | 5.3) [ "$output" = "$(printf '3 args: <1> <2> <3>\nstatus=0')" ] ;;
        *) false ;;
      esac
      ;;
    zsh) [ "$output" = "$(printf '3 args: <1> <2> <3>\nstatus=0')" ] ;;
    zsh-sh) [ "$output" = "$(printf '1 args: <{1..3}>\nstatus=0')" ] ;;
    dash | busybox | yash) [ "$output" = "status=2" ] ;;
    ksh93) [ "$output" = "status=3" ] ;;
    mksh | posh) [ "$output" = "status=1" ] ;;
    *) false ;;
  esac
}

@test "the word count is the product of the list lengths, and a range makes one word per step" {
  run_lab "$PROBE" count
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | ksh93)
      [ "${lines[0]}" = 81 ]
      [ "${lines[1]}" = 100000 ]
      ;;
    mksh)
      [ "${lines[0]}" = 81 ]
      [ "${lines[1]}" = 1 ]
      ;;
    dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = 1 ]
      [ "${lines[1]}" = 1 ]
      ;;
    *) false ;;
  esac
}

@test "a million generated words fit in the shell but not in execve: the external command fails" {
  run_lab "$PROBE" e2big
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | ksh93)
      [ "${lines[0]}" = "words=1000000" ]
      [ "${lines[1]}" = "status=126" ]
      ;;
    zsh)
      [ "${lines[0]}" = "words=1000000" ]
      [ "${lines[1]}" = "status=127" ]
      ;;
    mksh | dash | posh | busybox | yash | zsh-sh)
      [ "${lines[0]}" = "words=1" ]
      [ "${lines[1]}" = "status=0" ]
      ;;
    *) false ;;
  esac
}

@test "ksh93 alone accepts a printf format after a range" {
  run_lab "$PROBE" ksh-format
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    ksh93) [ "$output" = "3 args: <01> <02> <03>" ] ;;
    bash | zsh | mksh | dash | posh | busybox | yash | zsh-sh)
      [ "$output" = "1 args: <{1..3%02d}>" ]
      ;;
    *) false ;;
  esac
}

@test "zsh BRACE_CCL turns a comma-less brace word into a sorted character list" {
  run_lab "$PROBE" zsh-ccl
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh) [ "$output" = "3 args: <a> <b> <c>" ] ;;
    zsh-sh) [ "$output" = "1 args: <{cba}>" ] ;;
    bash | ksh93 | mksh | dash | posh | busybox | yash)
      [ "$output" = "no setopt builtin" ]
      ;;
    *) false ;;
  esac
}

# seq_flavor: print gnu, busybox, bsd, or unknown for the seq on PATH; the shell does not change it.
seq_flavor() {
  local out
  out="$(seq --version 2>&1 </dev/null) $(seq --help 2>&1 </dev/null)" || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *BusyBox*) echo busybox ;;
    *"usage: seq [-w] [-f format]"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

@test "seq, the external alternative, counts down only in the BSD implementation" {
  run_lab_c 'seq 3 1; echo "rc=$?"'
  [ "$status" -eq 0 ]
  case $(seq_flavor) in
    bsd) [ "$output" = "$(printf '3\n2\n1\nrc=0')" ] ;;
    gnu | busybox) [ "$output" = "rc=0" ] ;;
    *) false ;;
  esac
}

@test "an unknown mode is a usage error" {
  run_lab "$PROBE" no-such-mode
  [ "$status" -eq 2 ]
  [ "$output" = "usage: brace-probe.sh MODE [DIR]" ]
}
