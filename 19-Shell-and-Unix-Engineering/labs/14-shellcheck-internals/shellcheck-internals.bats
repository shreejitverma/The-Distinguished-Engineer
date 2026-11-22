#!/usr/bin/env bats
# ShellCheck internals: the pipeline the note describes (dialect, parse, directives, dataflow,
# source following, rc files, output formats), each step observed through the real binary.
# ShellCheck runs under the shell under test (LAB_SHELL) like any other command, so the matrix
# also proves the shell finds it; where a real shell and ShellCheck's model can disagree, the
# probe script runs under the shell under test and the families are asserted explicitly.
# Fixtures are written at run time into BATS_TEST_TMPDIR: they are deliberately defective, so
# they must never be lab files that the gate itself lints.

setup() {
  load ../lib/lab
  LINT="$BATS_TEST_DIRNAME/lint-shell.sh"
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  # An isolated HOME so a developer's ~/.shellcheckrc never changes a result.
  export HOME="$BATS_TEST_TMPDIR/home"
  mkdir -p "$HOME"
  unset SHELLCHECK_OPTS XDG_CONFIG_HOME
  W="$BATS_TEST_TMPDIR/w"
  mkdir -p "$W"
}

# lines_to FILE LINE...: write each LINE to FILE on its own line.
lines_to() {
  local file=$1
  shift
  printf '%s\n' "$@" >"$file"
}

# fixture NAME: write stdin to NAME inside the work directory.
fixture() {
  cat >"$W/$1"
}

# sc ARGS...: run ShellCheck under the shell under test, from SC_CWD (default: the work directory).
sc() {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'cd "$1" && shift && shellcheck "$@"' sc "${SC_CWD:-$W}" "$@"
}

# scn ARGS...: like sc, but ignoring every rc file.
scn() {
  sc --norc "$@"
}

# finding FILE POS CODE: succeed when the gcc-format $output reports CODE at POS, where POS is
# LINE or LINE:COLUMN.
finding() {
  local line
  while IFS= read -r line; do
    case $line in
      "$1:$2:"*"[$3]") return 0 ;;
      *) ;;
    esac
  done <<<"$output"
  return 1
}

# no_finding FILE POS CODE: the opposite of finding, failing the test when the finding exists.
no_finding() {
  ! finding "$@"
}

@test "the installed ShellCheck is 0.9.0 or newer, the release that added dataflow analysis" {
  run_lab_c 'shellcheck --version'
  [ "$status" -eq 0 ]
  local version=${output#*version: }
  version=${version%%$'\n'*}
  local major minor
  IFS=. read -r major minor _ <<<"$version"
  [ "$major" -eq 0 ]
  [ "$minor" -ge 9 ]
}

@test "the exit status separates clean, findings, unreadable input, bad usage, and bad option values" {
  fixture clean.sh <<'EOF'
#!/bin/sh
echo "$1"
EOF
  fixture dirty.sh <<'EOF'
#!/bin/sh
echo $1
EOF
  scn clean.sh
  [ "$status" -eq 0 ]
  scn dirty.sh
  [ "$status" -eq 1 ]
  scn no-such-file.sh
  [ "$status" -eq 2 ]
  scn --no-such-flag clean.sh
  [ "$status" -eq 3 ]
  scn
  [ "$status" -eq 3 ]
  scn -f no-such-format clean.sh
  [ "$status" -eq 4 ]
  scn -s zsh clean.sh
  [ "$status" -eq 4 ]
}

@test "a syntax error stops the analysis: only parse codes appear until the script parses" {
  fixture broken.sh <<'EOF'
#!/bin/sh
echo $1
if true; then
  echo hi
EOF
  fixture fixed.sh <<'EOF'
#!/bin/sh
echo $1
if true; then
  echo hi
fi
EOF
  scn -f gcc broken.sh
  [ "$status" -eq 1 ]
  finding broken.sh 3 SC1073
  no_finding broken.sh 2 SC2086
  scn -f gcc fixed.sh
  [ "$status" -eq 1 ]
  finding fixed.sh 2 SC2086
}

@test "a zsh shebang is refused with SC1071 and not analyzed, and a shell directive overrides it" {
  fixture z.sh <<'EOF'
#!/bin/zsh
echo $1
EOF
  fixture z-bash.sh <<'EOF'
#!/bin/zsh
# shellcheck shell=bash
echo $1
EOF
  scn -f gcc z.sh
  [ "$status" -eq 1 ]
  finding z.sh 1 SC1071
  no_finding z.sh 2 SC2086
  scn -f gcc z-bash.sh
  [ "$status" -eq 1 ]
  no_finding z-bash.sh 1 SC1071
  finding z-bash.sh 3 SC2086
}

@test "the dialect comes from a shell directive, the shebang, then the extension, and -s beats the shebang" {
  fixture sh-shebang.sh <<'EOF'
#!/bin/sh
[[ -n $1 ]]
EOF
  fixture sh-directive.sh <<'EOF'
#!/bin/sh
# shellcheck shell=bash
[[ -n $1 ]]
EOF
  fixture dash-shebang.sh <<'EOF'
#!/bin/dash
[[ -n $1 ]]
EOF
  fixture none.bash <<'EOF'
[[ -n $1 ]]
EOF
  fixture none.sh <<'EOF'
[[ -n $1 ]]
EOF
  scn -f gcc sh-shebang.sh
  finding sh-shebang.sh 2 SC3010
  scn -f gcc dash-shebang.sh
  finding dash-shebang.sh 2 SC3010
  scn -f gcc sh-directive.sh
  [ "$status" -eq 0 ]
  scn -f gcc -s bash sh-shebang.sh
  [ "$status" -eq 0 ]
  scn -f gcc none.bash
  [ "$status" -eq 0 ]
  scn -f gcc none.sh
  [ "$status" -eq 1 ]
  finding none.sh 1 SC2148
}

@test "only sh, ash, dash, bash, ksh, oksh, bats and busybox sh are accepted as shebang interpreters" {
  local pair interp expect got
  for pair in sh:none ash:SC2187 dash:none bash:none ksh:none ksh93:none oksh:none mksh:SC1008 yash:SC1008 posh:SC1008 "busybox sh:none" "busybox ash:SC1008" zsh:SC1071 bats:none; do
    interp=${pair%:*}
    expect=${pair##*:}
    lines_to "$W/shebang.sh" "#!/bin/$interp" true
    scn -f gcc shebang.sh
    got=$(grep -o '\[SC[0-9]*\]' <<<"$output" | tr -d '[]' || true)
    [ "${got:-none}" = "$expect" ]
  done
}

@test "a directive before the first command is file-wide" {
  fixture wide.sh <<'EOF'
#!/bin/bash

# shellcheck disable=SC2086
true
echo $1; echo $2
EOF
  scn -f gcc wide.sh
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "a later directive covers one AND-OR list: a && b is covered, a; b only its first command" {
  fixture scope.sh <<'EOF'
#!/bin/bash
true
# shellcheck disable=SC2086
echo $1 && echo $2
echo $3
# shellcheck disable=SC2086
echo $4; echo $5
EOF
  scn -f gcc scope.sh
  [ "$status" -eq 1 ]
  no_finding scope.sh 4 SC2086
  finding scope.sh 5:6 SC2086
  no_finding scope.sh 7:6 SC2086
  finding scope.sh 7:15 SC2086
}

@test "a directive in front of an elif is a parse error, because directives bind to whole commands" {
  fixture elif.sh <<'EOF'
#!/bin/bash
true
if true; then
  :
# shellcheck disable=SC2086
elif true; then
  echo $1
fi
EOF
  scn -f gcc elif.sh
  [ "$status" -eq 1 ]
  finding elif.sh 5:1 SC1123
}

@test "dataflow analysis treats a literal without blanks or glob characters as clean" {
  fixture lit.sh <<'EOF'
#!/bin/sh
a=hello
echo $a
EOF
  scn -f gcc lit.sh
  [ "$status" -eq 0 ]
  scn -f gcc --extended-analysis=false lit.sh
  [ "$status" -eq 1 ]
  finding lit.sh 3:6 SC2086
  scn -f gcc -o quote-safe-variables lit.sh
  finding lit.sh 3:6 SC2248
  no_finding lit.sh 3:6 SC2086
}

@test "dataflow analysis follows branches and function calls and keeps the last value on each path" {
  fixture branch.sh <<'EOF'
#!/bin/sh
if [ -n "$1" ]; then a="x y"; else a=1; fi
echo $a
EOF
  fixture func.sh <<'EOF'
#!/bin/sh
f() { a="x y"; }
f
echo $a
EOF
  fixture overwritten.sh <<'EOF'
#!/bin/sh
a="x y"
a=1
echo $a
EOF
  fixture overwritten-in-func.sh <<'EOF'
#!/bin/sh
a="x y"
f() { a=1; }
f
echo $a
EOF
  scn -f gcc branch.sh
  finding branch.sh 3 SC2086
  scn -f gcc func.sh
  finding func.sh 4 SC2086
  scn -f gcc overwritten.sh
  [ "$status" -eq 0 ]
  scn -f gcc overwritten-in-func.sh
  [ "$status" -eq 0 ]
}

@test "ShellCheck calls a value clean without looking at IFS, and the real shell then splits it" {
  fixture ifs.sh <<'EOF'
#!/bin/sh
v=a:b
IFS=:
set -- $v
echo "$#"
EOF
  scn -f gcc ifs.sh
  [ "$status" -eq 0 ]
  [ -z "$output" ]
  run_lab "$PROBE" ifs-split
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh) [ "$output" = 1 ] ;; # native zsh never splits an unquoted parameter
    bash | zsh-sh | dash | busybox | ksh93 | mksh | posh | yash) [ "$output" = 2 ] ;;
    *) false ;;
  esac
}

@test "ShellCheck models the last pipeline stage as a subshell except for ksh and a lastpipe option anywhere" {
  local dialect
  for dialect in /bin/sh /bin/bash /bin/dash; do
    lines_to "$W/lp.sh" "#!$dialect" 'echo hi | read -r v' "echo \"\$v\""
    scn -f gcc lp.sh
    finding lp.sh 2 SC2030
    finding lp.sh 3 SC2031
  done
  lines_to "$W/lp-ksh.sh" '#!/bin/ksh' 'echo hi | read -r v' "echo \"\$v\""
  scn -f gcc lp-ksh.sh
  [ "$status" -eq 0 ]
  fixture lp-late.sh <<'EOF'
#!/bin/bash
echo hi | read -r v
shopt -s lastpipe
echo "$v"
EOF
  scn -f gcc lp-late.sh
  [ "$status" -eq 0 ]
}

@test "real shells disagree on whether the last pipeline stage runs in the current shell" {
  run_lab "$PROBE" lastpipe
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh | ksh93) [ "$output" = hi ] ;;
    bash | zsh-sh | dash | busybox | mksh | posh | yash) [ "$output" = unset ] ;;
    *) false ;;
  esac
}

@test "the probe rejects an unknown mode" {
  run_lab "$PROBE" no-such-mode
  [ "$status" -eq 2 ]
}

@test "source following is off by default, and -x makes the sourced assignment count" {
  mkdir -p "$W/src"
  fixture src/lib.sh <<'EOF'
#!/bin/bash
lib_var="a b"
EOF
  fixture main.sh <<'EOF'
#!/bin/bash
source ./src/lib.sh
echo "$lib_var"
EOF
  scn -f gcc main.sh
  finding main.sh 2 SC1091
  finding main.sh 3 SC2154
  scn -f gcc -x main.sh
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "directives silence or redirect a source without making ShellCheck read the file" {
  mkdir -p "$W/src"
  fixture src/lib.sh <<'EOF'
#!/bin/bash
lib_var="a b"
EOF
  fixture devnull.sh <<'EOF'
#!/bin/bash
# shellcheck source=/dev/null
source ./src/lib.sh
echo "$lib_var"
EOF
  fixture dynamic.sh <<'EOF'
#!/bin/bash
source "$1"
echo "$lib_var"
EOF
  fixture hinted.sh <<'EOF'
#!/bin/bash
# shellcheck source=src/lib.sh
source "$1"
echo "$lib_var"
EOF
  scn -f gcc devnull.sh
  no_finding devnull.sh 3 SC1091
  finding devnull.sh 4 SC2154
  scn -f gcc dynamic.sh
  finding dynamic.sh 2 SC1090
  scn -f gcc -x hinted.sh
  [ "$status" -eq 0 ]
  scn -f gcc hinted.sh
  finding hinted.sh 3 SC1091
}

@test "a relative source is resolved from the working directory, and source-path SCRIPTDIR fixes that" {
  mkdir -p "$W/src"
  fixture src/lib.sh <<'EOF'
#!/bin/bash
lib_var="a b"
EOF
  fixture main.sh <<'EOF'
#!/bin/bash
source ./src/lib.sh
echo "$lib_var"
EOF
  fixture main-scriptdir.sh <<'EOF'
#!/bin/bash
# shellcheck source-path=SCRIPTDIR
source ./src/lib.sh
echo "$lib_var"
EOF
  SC_CWD=$BATS_TEST_TMPDIR scn -f gcc -x w/main.sh
  finding w/main.sh 2 SC1091
  finding w/main.sh 3 SC2154
  SC_CWD=$BATS_TEST_TMPDIR scn -f gcc -x w/main-scriptdir.sh
  [ "$status" -eq 0 ]
}

@test "only the nearest rc file is read, --norc ignores it, and --rcfile replaces the search" {
  mkdir -p "$W/proj/sub"
  printf 'disable=SC2086\n' >"$W/proj/.shellcheckrc"
  printf 'enable=add-default-case\n' >"$W/proj/sub/.shellcheckrc"
  fixture proj/sub/t.sh <<'EOF'
#!/bin/sh
echo $1
case $1 in a) : ;; esac
EOF
  sc -f gcc proj/sub/t.sh
  finding proj/sub/t.sh 2 SC2086
  finding proj/sub/t.sh 3 SC2249
  rm "$W/proj/sub/.shellcheckrc"
  sc -f gcc proj/sub/t.sh
  [ "$status" -eq 0 ]
  scn -f gcc proj/sub/t.sh
  finding proj/sub/t.sh 2 SC2086
  printf 'enable=add-default-case\n' >"$W/other.rc"
  sc -f gcc --rcfile other.rc proj/sub/t.sh
  finding proj/sub/t.sh 2 SC2086
  finding proj/sub/t.sh 3 SC2249
}

@test "optional checks are listed by name and stay off until enabled" {
  run_lab_c 'shellcheck --list-optional'
  [ "$status" -eq 0 ]
  local name
  for name in add-default-case avoid-nullary-conditions check-extra-masked-returns check-set-e-suppressed deprecate-which quote-safe-variables; do
    printf '%s\n' "$output" | grep -Eq "^name: +$name\$"
  done
  fixture masked.sh <<'EOF'
#!/bin/bash
rm -r "$(get_dir)/home"
EOF
  scn -f gcc masked.sh
  no_finding masked.sh 2 SC2312
  scn -f gcc -o check-extra-masked-returns masked.sh
  finding masked.sh 2 SC2312
}

@test "ShellCheck knows the variables that bats' run sets, and a plain script does not" {
  # bats rewrites every line that starts with @test, even inside a heredoc, so the fixture spells
  # the keyword TEST and the real one is substituted when the file is written.
  local body
  body=$(
    cat <<'EOF'
#!/usr/bin/env bats

TEST "x" {
  run echo hi
  [ "$status" -eq 0 ]
  [ "$output" = hi ]
}
EOF
  )
  printf '%s\n' "${body//TEST /@test }" >"$W/t.bats"
  fixture t.sh <<'EOF'
#!/bin/bash
run echo hi
[ "$status" -eq 0 ]
[ "$output" = hi ]
EOF
  scn -f gcc t.bats
  [ "$status" -eq 0 ]
  scn -f gcc t.sh
  finding t.sh 3 SC2154
  finding t.sh 4 SC2154
}

@test "command checks key on the literal command name, so a variable name hides the command" {
  fixture rm-literal.sh <<'EOF'
#!/bin/bash
d=$1
rm -rf "$d/"*
EOF
  fixture rm-path.sh <<'EOF'
#!/bin/bash
d=$1
/bin/rm -rf "$d/"*
EOF
  fixture rm-variable.sh <<'EOF'
#!/bin/bash
d=$1
r=rm
"$r" -rf "$d/"*
EOF
  scn -f gcc rm-literal.sh
  finding rm-literal.sh 3 SC2115
  scn -f gcc rm-path.sh
  finding rm-path.sh 3 SC2115
  scn -f gcc rm-variable.sh
  no_finding rm-variable.sh 4 SC2115
}

@test "fixes are emitted as a unified diff and as json1 replacements, and the fixed script is clean" {
  fixture dirty.sh <<'EOF'
#!/bin/sh
echo $1
EOF
  scn -f diff dirty.sh
  [ "$status" -eq 1 ]
  [[ $output == *$'-echo $1\n+echo "$1"'* ]]
  scn -f json1 dirty.sh
  [[ $output == *'"code":2086'* ]]
  [[ $output == *'"replacements"'* ]]
  fixture dirty-fixed.sh <<'EOF'
#!/bin/sh
echo "$1"
EOF
  scn dirty-fixed.sh
  [ "$status" -eq 0 ]
}

@test "json1 counts a tab as one column and the legacy json format counts a tab stop of eight" {
  lines_to "$W/tab.sh" '#!/bin/sh' $'\techo '"\$1"
  scn -f json1 tab.sh
  [[ $output == *'"column":7'* ]]
  scn -f json tab.sh
  [[ $output == *'"column":14'* ]]
}

@test "lint-shell.sh selects files by shebang, skips zsh and non-scripts, and reports findings with status 1" {
  local tree=$W/tree
  mkdir -p "$tree/bin" "$tree/lib"
  lines_to "$tree/bin/tool" '#!/bin/bash' "echo \"\$1\""
  lines_to "$tree/bin/z" '#!/usr/bin/env zsh' "echo \$1"
  lines_to "$tree/lib/a.sh" '#!/bin/sh' "echo \$1"
  lines_to "$tree/lib/b" '#!/usr/bin/env bash' "echo \"\$1\""
  lines_to "$tree/noshebang.sh" "echo \$1"
  printf 'plain text\n' >"$tree/notes.txt"
  run_lab "$LINT" "$tree"
  [ "$status" -eq 1 ]
  [ "${#lines[@]}" -eq 8 ]
  [ "${lines[0]}" = "ok $tree/bin/tool" ]
  [ "${lines[1]}" = "skip $tree/bin/z" ]
  [ "${lines[2]}" = "fail $tree/lib/a.sh" ]
  [[ ${lines[3]} == "$tree/lib/a.sh:2:6: "*"[SC2086]" ]]
  [ "${lines[4]}" = "ok $tree/lib/b" ]
  [ "${lines[5]}" = "skip $tree/noshebang.sh" ]
  [ "${lines[6]}" = "skip $tree/notes.txt" ]
  [ "${lines[7]}" = "checked 3, failed 1, skipped 3" ]
}

@test "lint-shell.sh exits 0 on a clean tree" {
  mkdir -p "$W/tree"
  lines_to "$W/tree/ok.sh" '#!/bin/sh' "echo \"\$1\""
  run_lab "$LINT" "$W/tree"
  [ "$status" -eq 0 ]
  [ "${lines[1]}" = "checked 1, failed 0, skipped 0" ]
}

@test "lint-shell.sh maps a ShellCheck invocation failure to status 2, not to findings" {
  mkdir -p "$W/tree" "$W/stub"
  lines_to "$W/tree/ok.sh" '#!/bin/sh' "echo \"\$1\""
  printf '#!/bin/sh\nexit 3\n' >"$W/stub/shellcheck"
  chmod +x "$W/stub/shellcheck"
  PATH=$W/stub:$PATH
  run_lab "$LINT" "$W/tree"
  [ "$status" -eq 2 ]
  [[ $output == *"shellcheck exited 3"* ]]
}

@test "lint-shell.sh refuses a missing directory and a missing argument with status 2" {
  run_lab "$LINT" "$W/no-such-dir"
  [ "$status" -eq 2 ]
  [[ $output == *"usage:"* ]]
  run_lab "$LINT"
  [ "$status" -eq 2 ]
}
