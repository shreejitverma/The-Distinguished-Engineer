#!/usr/bin/env bats
# Proves the configuration precedence ladder that resolve.sh implements (defaults, system
# files, NO_COLOR, user file, DEMO_* environment, options), the parsing rules that keep a
# config file data, and what each shell does when a script loads config with the dot built-in.

# shellcheck disable=SC2016 # code passed to run_lab_c is expanded by the shell under test

setup() {
  load ../lib/lab
  RESOLVE="$BATS_TEST_DIRNAME/resolve.sh"
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  # The ambient environment is a configuration layer too: clear it so every test is hermetic.
  unset NO_COLOR DEMO_PORT DEMO_LOG_LEVEL DEMO_COLOR DEMO_GREETING DEMO_CONFIG
  T=$(mktemp -d "$BATS_TEST_TMPDIR/cp.XXXXXX")
  export HOME="$T/home"
  export XDG_CONFIG_DIRS="$T/sys1:$T/sys2"
  export XDG_CONFIG_HOME="$T/user"
  mkdir -p "$HOME" "$T/sys1/demo" "$T/sys2/demo" "$T/user/demo" "$T/empty-bin"
}

# expect_output EXPECTED: fail unless $output equals EXPECTED exactly.
expect_output() {
  if [ "$output" != "$1" ]; then
    printf 'expected:\n%s\nactual:\n%s\n' "$1" "$output" >&2
    return 1
  fi
}

# set_last: set last to the last line of $output (diagnostics on stderr come first).
set_last() {
  last=${output##*$'\n'}
}

# bash_posix_mode: succeed if the shell under test is bash running in POSIX mode.
bash_posix_mode() {
  run_lab_c 'case :${SHELLOPTS-}: in *:posix:*) echo posix ;; *) echo normal ;; esac'
  [ "$output" = posix ]
}

# bash_major: print the major version of the shell under test when it is bash.
bash_major() {
  local v
  v=$(lab_shell_version)
  printf '%s\n' "${v%%.*}"
}

@test "with no files and no environment every value is the built-in default" {
  run_lab "$RESOLVE"
  [ "$status" -eq 0 ]
  expect_output 'port=8080 (default)
log_level=info (default)
color=auto (default)
greeting=hello (default)'
}

@test "each layer overrides the one below it: system file, user file, environment, option" {
  printf 'port = 1001\nlog_level = debug\ncolor = always\ngreeting = sys\n' >"$T/sys1/demo/config"
  printf 'log_level = warn\ncolor = never\ngreeting = user\n' >"$T/user/demo/config"
  DEMO_COLOR=always DEMO_GREETING=env run_lab "$RESOLVE" --greeting cli
  [ "$status" -eq 0 ]
  expect_output "port=1001 ($T/sys1/demo/config:1)
log_level=warn ($T/user/demo/config:1)
color=always (env DEMO_COLOR)
greeting=cli (option --greeting)"
}

@test "the first XDG_CONFIG_DIRS entry is the most important, so it is read last" {
  printf 'port = 1111\n' >"$T/sys1/demo/config"
  printf 'port = 2222\nlog_level = error\n' >"$T/sys2/demo/config"
  run_lab "$RESOLVE"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "port=1111 ($T/sys1/demo/config:1)" ]
  [ "${lines[1]}" = "log_level=error ($T/sys2/demo/config:2)" ]
}

@test "with nothing set the search uses /etc/xdg and HOME/.config, reading nothing" {
  unset XDG_CONFIG_DIRS XDG_CONFIG_HOME
  run_lab "$RESOLVE" --print-files
  [ "$status" -eq 0 ]
  expect_output "system /etc/xdg/demo/config
user $HOME/.config/demo/config"
}

@test "empty and relative XDG entries are ignored, and an empty XDG_CONFIG_HOME means HOME/.config" {
  XDG_CONFIG_DIRS="relative/dir::$T/sys1:" XDG_CONFIG_HOME='' run_lab "$RESOLVE" --print-files
  [ "$status" -eq 0 ]
  expect_output "system $T/sys1/demo/config
user $HOME/.config/demo/config"
  XDG_CONFIG_HOME='relative/dir' run_lab "$RESOLVE" --print-files
  [ "${lines[2]}" = "user $HOME/.config/demo/config" ]
}

@test "an empty DEMO_PORT is treated as unset and does not override the file" {
  printf 'port = 7000\n' >"$T/user/demo/config"
  DEMO_PORT='' run_lab "$RESOLVE"
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "port=7000 ($T/user/demo/config:1)" ]
}

@test "NO_COLOR beats system files, loses to the user file, DEMO_COLOR and --color" {
  printf 'color = always\n' >"$T/sys1/demo/config"
  NO_COLOR=1 run_lab "$RESOLVE"
  [ "${lines[2]}" = "color=never (env NO_COLOR)" ]
  printf 'color = auto\n' >"$T/user/demo/config"
  NO_COLOR=1 run_lab "$RESOLVE"
  [ "${lines[2]}" = "color=auto ($T/user/demo/config:1)" ]
  NO_COLOR=1 DEMO_COLOR=always run_lab "$RESOLVE"
  [ "${lines[2]}" = "color=always (env DEMO_COLOR)" ]
  NO_COLOR=1 DEMO_COLOR=always run_lab "$RESOLVE" --color never
  [ "${lines[2]}" = "color=never (option --color)" ]
}

@test "a repeated option is applied in command-line order, so the last one wins" {
  run_lab "$RESOLVE" --port 1 --log-level debug --port 2
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "port=2 (option --port)" ]
  [ "${lines[1]}" = "log_level=debug (option --log-level)" ]
}

@test "--config replaces both file layers wherever it appears, and beats DEMO_CONFIG" {
  printf 'port = 1001\ngreeting = sys\n' >"$T/sys1/demo/config"
  printf 'log_level = warn\n' >"$T/user/demo/config"
  printf 'port = 6000\nlog_level = error\n' >"$T/explicit.conf"
  printf 'port = 6111\n' >"$T/env.conf"
  DEMO_CONFIG="$T/env.conf" run_lab "$RESOLVE" --port 5 --config "$T/explicit.conf"
  [ "$status" -eq 0 ]
  expect_output "port=5 (option --port)
log_level=error ($T/explicit.conf:2)
color=auto (default)
greeting=hello (default)"
  DEMO_CONFIG="$T/env.conf" run_lab "$RESOLVE"
  [ "${lines[0]}" = "port=6111 ($T/env.conf:1)" ]
}

@test "an explicit config file must exist, while a missing implicit file is skipped" {
  run_lab "$RESOLVE" --config "$T/nope.conf"
  [ "$status" -eq 2 ]
  expect_output "resolve.sh: config file '$T/nope.conf' is not a readable file"
  run_lab "$RESOLVE" --config "$T/nope.conf" --no-config
  [ "$status" -eq 2 ]
  expect_output 'resolve.sh: --config and --no-config are mutually exclusive'
}

@test "--no-config ignores every file but keeps environment and options" {
  printf 'port = 1001\n' >"$T/sys1/demo/config"
  printf 'log_level = warn\n' >"$T/user/demo/config"
  DEMO_LOG_LEVEL=error run_lab "$RESOLVE" --no-config
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "port=8080 (default)" ]
  [ "${lines[1]}" = "log_level=error (env DEMO_LOG_LEVEL)" ]
}

@test "parsing: whitespace, = and # in values, CRLF, duplicates, and a last line without a newline" {
  printf '# comment\n\n  port\t=  7001 \r\ngreeting = a=b # not a comment\r\nport = 7002\nlog_level=debug' \
    >"$T/user/demo/config"
  run_lab "$RESOLVE"
  [ "$status" -eq 0 ]
  expect_output "port=7002 ($T/user/demo/config:5)
log_level=debug ($T/user/demo/config:6)
color=auto (default)
greeting=a=b # not a comment ($T/user/demo/config:4)"
}

@test "an unknown key, a bad value, or a line without = fails with file and line" {
  printf 'port = 1\nprot = 2\n' >"$T/user/demo/config"
  run_lab "$RESOLVE"
  [ "$status" -eq 2 ]
  expect_output "resolve.sh: $T/user/demo/config:2: unknown key 'prot'"
  printf 'port = 080\n' >"$T/user/demo/config"
  run_lab "$RESOLVE"
  [ "$status" -eq 2 ]
  expect_output "resolve.sh: port '080' from $T/user/demo/config:1 is not an integer from 1 to 65535"
  printf 'just words\n' >"$T/user/demo/config"
  run_lab "$RESOLVE"
  [ "$status" -eq 2 ]
  expect_output "resolve.sh: $T/user/demo/config:1: expected KEY = VALUE"
  run_lab "$RESOLVE" --no-config --port 70000
  [ "$status" -eq 2 ]
  expect_output "resolve.sh: port '70000' from option --port is not an integer from 1 to 65535"
}

@test "a bad value in a lower layer fails even when a higher layer overrides it" {
  printf 'port = abc\n' >"$T/sys2/demo/config"
  run_lab "$RESOLVE" --port 1
  [ "$status" -eq 2 ]
  expect_output "resolve.sh: port 'abc' from $T/sys2/demo/config:1 is not an integer from 1 to 65535"
}

@test "the resolver keeps a command substitution as text, while the dot built-in runs it" {
  printf 'greeting=$(echo ran >%s/ran.marker)hi\n' "$T" >"$T/evil.conf"
  run_lab "$RESOLVE" --config "$T/evil.conf"
  [ "$status" -eq 0 ]
  [ "${lines[3]}" = "greeting=\$(echo ran >$T/ran.marker)hi ($T/evil.conf:1)" ]
  [ ! -e "$T/ran.marker" ]
  run_lab_c '. "$1"; echo "greeting=$greeting"' sh "$T/evil.conf"
  [ "$status" -eq 0 ]
  expect_output 'greeting=hi'
  [ -f "$T/ran.marker" ]
}

@test "a naive read loop drops a last line that has no newline; the || test keeps it" {
  printf 'a=1\nb=2' >"$T/noeol.conf"
  run_lab_c 'n=0; while IFS= read -r l; do n=$((n + 1)); done <"$1"; echo "naive=$n"
    n=0; while IFS= read -r l || [ -n "$l" ]; do n=$((n + 1)); done <"$1"; echo "fixed=$n"' \
    sh "$T/noeol.conf"
  [ "$status" -eq 0 ]
  expect_output 'naive=1
fixed=2'
}

@test "sourcing a file that assigns overwrites the environment; := in the file defers to it" {
  printf 'port=9000\n' >"$T/naive.conf"
  printf ': "${port:=9000}"\n' >"$T/guarded.conf"
  DEMO_PORT=1 run_lab "$PROBE" naive-source "$T"
  [ "$status" -eq 0 ]
  expect_output 'naive port=9000'
  DEMO_PORT=1 run_lab "$PROBE" guarded-source "$T"
  [ "$status" -eq 0 ]
  expect_output 'guarded port=1'
  run_lab "$PROBE" guarded-source "$T"
  [ "$status" -eq 0 ]
  expect_output 'guarded port=9000'
}

@test "dot with a missing file aborts a non-interactive POSIX shell; bash and zsh go on" {
  local family posix=no
  family=$(lab_shell_family)
  if [ "$family" = bash ] && bash_posix_mode; then posix=yes; fi
  run_lab "$PROBE" dot-missing "$T"
  case $family in
    bash)
      if [ "$posix" = yes ]; then
        [ "$status" -ne 0 ]
        [[ $output != *survived* ]]
      else
        [ "$status" -eq 0 ]
        set_last
        [ "$last" = "survived status=1" ]
      fi
      ;;
    zsh)
      [ "$status" -eq 0 ]
      set_last
      [ "$last" = "survived status=127" ]
      ;;
    zsh-sh | dash | ksh93 | busybox | yash | mksh | posh)
      [ "$status" -ne 0 ]
      [[ $output != *survived* ]]
      ;;
    *) false ;;
  esac
}

@test "dot with a slash-free name searches PATH only, except bash and yash outside POSIX mode" {
  local family posix=no major=0
  family=$(lab_shell_family)
  if [ "$family" = bash ]; then
    major=$(bash_major)
    if bash_posix_mode; then posix=yes; fi
  fi
  printf 'cwd_conf=loaded-from-cwd\n' >"$T/demo-cwd-only.conf"
  run_lab "$PROBE" dot-cwd "$T"
  case $family in
    bash)
      # bash 3.2 searches the current directory in POSIX mode too; 4.0 and later do not.
      if [ "$posix" = no ] || [ "$major" -lt 4 ]; then
        [ "$status" -eq 0 ]
        expect_output 'loaded-from-cwd
after status=0'
      else
        [ "$status" -ne 0 ]
        [[ $output != *loaded-from-cwd* ]]
        [[ $output != *after* ]]
      fi
      ;;
    yash)
      [ "$status" -eq 0 ]
      expect_output 'loaded-from-cwd
after status=0'
      ;;
    zsh)
      [ "$status" -eq 0 ]
      [[ $output != *loaded-from-cwd* ]]
      set_last
      [ "$last" = "after status=127" ]
      ;;
    busybox)
      # A build option: Alpine sets ASH_BASH_SOURCE_CURDIR=y and loads the file, Debian keeps
      # the Kconfig default n and aborts. Either way it is all or nothing, never half of each.
      if [ "$status" -eq 0 ]; then
        expect_output 'loaded-from-cwd
after status=0'
      else
        [[ $output != *loaded-from-cwd* ]]
        [[ $output != *after* ]]
      fi
      ;;
    zsh-sh | dash | ksh93 | mksh | posh)
      [ "$status" -ne 0 ]
      [[ $output != *loaded-from-cwd* ]]
      [[ $output != *after* ]]
      ;;
    *) false ;;
  esac
}

@test "a syntax error late in a dotted file can leave the earlier lines applied" {
  local family
  family=$(lab_shell_family)
  printf 'echo line1-ran\n\nif\n' >"$T/late-error.conf"
  run_lab "$PROBE" dot-late-error "$T"
  case $family in
    bash | zsh | zsh-sh | posh)
      [ "${lines[0]}" = line1-ran ]
      set_last
      [[ $last = "after status="[1-9]* ]]
      ;;
    dash | busybox | yash)
      [ "$status" -ne 0 ]
      [ "${lines[0]}" = line1-ran ]
      [[ $output != *after* ]]
      ;;
    ksh93)
      [[ $output != *line1-ran* ]]
      set_last
      [[ $last = "after status="[1-9]* ]]
      ;;
    mksh)
      # Line 1 runs in every mksh; Debian's R59 2025/04/26 then aborts, while Alpine's
      # R59 2020/10/31 continues the script.
      [ "${lines[0]}" = line1-ran ]
      case $(lab_shell_version) in
        *2020/*)
          set_last
          [[ $last = "after status="[1-9]* ]]
          ;;
        *)
          [ "$status" -ne 0 ]
          [[ $output != *after* ]]
          ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "BASH_ENV in the environment runs code before a non-POSIX bash script starts" {
  local family posix=no
  family=$(lab_shell_family)
  if [ "$family" = bash ] && bash_posix_mode; then posix=yes; fi
  printf 'echo hook-ran\n' >"$T/hook.sh"
  BASH_ENV="$T/hook.sh" run_lab_c 'echo main'
  [ "$status" -eq 0 ]
  case $family in
    bash)
      if [ "$posix" = yes ]; then
        expect_output main
      else
        expect_output 'hook-ran
main'
      fi
      ;;
    zsh | zsh-sh | dash | ksh93 | busybox | yash | mksh | posh) expect_output main ;;
    *) false ;;
  esac
}

@test "SHELLOPTS in the environment switches options on in a child bash, and only in bash" {
  local family
  family=$(lab_shell_family)
  : >"$T/star-file"
  lab_shell_words
  run_lab_c 'cd "$1" && shift && exec env SHELLOPTS=noglob "$@" -c "echo star-*"' \
    sh "$T" "${LAB_SHELL_WORDS[@]}"
  [ "$status" -eq 0 ]
  case $family in
    bash) expect_output 'star-*' ;;
    zsh | zsh-sh | dash | ksh93 | busybox | yash | mksh | posh) expect_output star-file ;;
    *) false ;;
  esac
}
