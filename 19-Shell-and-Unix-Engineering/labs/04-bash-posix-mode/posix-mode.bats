#!/usr/bin/env bats
# Verification lab for Bash POSIX Mode: what `set -o posix` changes in bash 3.2 to 5.3,
# how a shell enters its POSIX personality, and what the other shells do natively.
# Under bash every probe runs the snippet twice, in a fresh default-mode bash and a fresh
# POSIX-mode bash of the same binary; under any other shell it runs once, natively.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/posix-probe.sh"
  export TMPDIR="$BATS_TEST_TMPDIR"
}

# series: print the release series this lab measured for the shell under test:
# 3.2, 4.4, 5.1-5.2, or 5.3 for bash; 93u+m or 93u+2012 for ksh93; "unmeasured" otherwise,
# so an arm that needs a series fails for a release nobody has measured.
series() {
  local family version
  family=$(lab_shell_family)
  version=$(lab_shell_version)
  case $family:$version in
    bash:3.2.*) echo 3.2 ;;
    bash:4.4.*) echo 4.4 ;;
    bash:5.1.* | bash:5.2.*) echo 5.1-5.2 ;;
    bash:5.3.*) echo 5.3 ;;
    ksh93:*93u+m*) echo 93u+m ;;
    "ksh93:"*"93u+ 2012"*) echo 93u+2012 ;;
    *) echo unmeasured ;;
  esac
}

# expect WANT: fail unless the probe exited 0 and printed exactly WANT; show both on failure.
expect() {
  printf 'got:  %s (status %s)\nwant: %s\n' "$output" "$status" "$1"
  [ "$status" -eq 0 ]
  [ "$output" = "$1" ]
}

@test "personality: only bash, ksh93, mksh and yash have a posix option, and plain bash starts with it off" {
  run_lab "$PROBE" personality
  lab_shell_words
  case $(lab_shell_family) in
    bash)
      case ${LAB_SHELL_WORDS[0]##*/} in
        sh) want='posix=0 plc=1 emulate=none' ;; # macOS /bin/sh is bash 3.2 started as sh
        *) want='posix=1 plc=1 emulate=none' ;;
      esac
      ;;
    ksh93 | mksh | yash) want='posix=1 plc=1 emulate=none' ;;
    zsh) want='posix=2 plc=2 emulate=zsh' ;;
    zsh-sh) want='posix=2 plc=2 emulate=sh' ;;
    dash | posh | busybox) want='posix=2 plc=2 emulate=none' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "personality: POSIXLY_CORRECT in the environment switches bash, and only bash, to POSIX mode" {
  export POSIXLY_CORRECT=y
  run_lab "$PROBE" personality
  case $(lab_shell_family) in
    bash) want='posix=0 plc=1 emulate=none' ;;
    ksh93 | mksh | yash) want='posix=1 plc=1 emulate=none' ;;
    zsh) want='posix=2 plc=2 emulate=zsh' ;;
    zsh-sh) want='posix=2 plc=2 emulate=sh' ;;
    dash | posh | busybox) want='posix=2 plc=2 emulate=none' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "personality: started under the name sh, bash, ksh93u+m, yash and zsh switch to their POSIX personality" {
  local real
  lab_shell_words
  real=$(command -v "${LAB_SHELL_WORDS[0]}")
  ln -s "$real" "$BATS_TEST_TMPDIR/sh"
  # The shell under test execs its own binary renamed sh (BusyBox then runs its sh applet, ash).
  # shellcheck disable=SC2016 # $1 and $2 belong to the shell under test
  run_lab_c 'exec "$1" "$2" personality' probe "$BATS_TEST_TMPDIR/sh" "$PROBE"
  case $(lab_shell_family) in
    bash) want='posix=0 plc=1 emulate=none' ;;
    ksh93)
      case $(series) in
        93u+m) want='posix=0 plc=1 emulate=none' ;;
        93u+2012) want='posix=1 plc=1 emulate=none' ;; # 93u+ 2012 has no posix option
        *) false ;;
      esac
      ;;
    yash) want='posix=0 plc=0 emulate=none' ;; # yash takes -o posix as an abbreviation of posixlycorrect
    zsh | zsh-sh) want='posix=2 plc=2 emulate=sh' ;;
    mksh) want='posix=1 plc=1 emulate=none' ;;
    dash | posh | busybox) want='posix=2 plc=2 emulate=none' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "assign: an assignment before a special built-in persists in POSIX mode, not in default bash or native zsh" {
  run_lab "$PROBE" diff assign
  case $(lab_shell_family) in
    bash) want='default=unset/0 posix=1/0' ;;
    zsh) want='native=unset/0' ;;
    zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) want='native=1/0' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "exportp: export -p prints declare -x in default bash and the POSIX export format in POSIX mode" {
  run_lab "$PROBE" diff exportp
  case $(lab_shell_family) in
    bash) want='default=declare -x y="1"/0 posix=export y="1"/0' ;;
    dash | busybox) want="native=export y='1'/0" ;;
    zsh | zsh-sh | yash | posh | mksh | ksh93) want='native=export y=1/0' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "trap: trap with no operands names signals without the SIG prefix only in POSIX mode" {
  run_lab "$PROBE" diff trap
  case $(lab_shell_family) in
    bash) want="default=trap -- 'echo hi' SIGINT/0 posix=trap -- 'echo hi' INT/0" ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) want="native=trap -- 'echo hi' INT/0" ;;
    *) false ;;
  esac
  expect "$want"
}

@test "tilde: default bash expands a=~/x in an ordinary argument, POSIX mode leaves it literal" {
  run_lab "$PROBE" diff tilde
  case $(lab_shell_family) in
    bash) want='default=expanded/0 posix=literal/0' ;;
    mksh) want='native=expanded/0' ;;
    zsh | zsh-sh | yash | busybox | posh | ksh93 | dash) want='native=literal/0' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "alias: a non-interactive bash expands aliases only in POSIX mode" {
  run_lab "$PROBE" diff alias
  case $(lab_shell_family) in
    bash) want='default=/127 posix=aliased/0' ;;
    posh) want='native=/127' ;; # posh has no alias builtin
    zsh | zsh-sh | yash | busybox | mksh | ksh93 | dash) want='native=aliased/0' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "errexit: POSIX mode makes a command substitution inherit set -e, so the failed assignment exits" {
  run_lab "$PROBE" diff errexit
  case $(lab_shell_family) in
    bash) want='default=[survived]/0 posix=/1' ;;
    busybox) want='native=[survived]/0' ;;
    zsh | zsh-sh | yash | posh | mksh | ksh93 | dash) want='native=/1' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "toggle: set -o posix then set +o posix leaves inherit_errexit on in bash 4.4 and later" {
  run_lab "$PROBE" diff toggle
  case $(lab_shell_family) in
    bash)
      case $(series) in
        3.2) want='default=[survived]/0 posix=[survived]/0' ;; # 3.2 reads posixly_correct directly
        4.4 | 5.1-5.2 | 5.3) want='default=/1 posix=/1' ;;
        *) false ;;
      esac
      ;;
    busybox) want='native=[survived]/0' ;; # set -o posix fails without exiting; -e is not inherited
    zsh | zsh-sh | yash | posh | mksh) want='native=/1' ;;
    ksh93)
      case $(series) in
        93u+m) want='native=/1' ;;
        93u+2012) want='native=/2' ;; # no posix option: set exits the shell
        *) false ;;
      esac
      ;;
    dash) want='native=/2' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "scoped: local - restores every POSIX-mode side effect on return only from bash 5.1" {
  run_lab "$PROBE" diff scoped
  case $(lab_shell_family) in
    bash)
      case $(series) in
        3.2 | 4.4) want='default=/1 posix=/1' ;;
        5.1-5.2 | 5.3) want='default=[survived]/0 posix=/1' ;;
        *) false ;;
      esac
      ;;
    busybox) want='native=[survived]/0' ;;
    zsh | zsh-sh | yash | posh | mksh) want='native=/1' ;;
    ksh93)
      case $(series) in
        93u+m) want='native=/1' ;;
        93u+2012) want='native=/2' ;;
        *) false ;;
      esac
      ;;
    dash) want='native=/2' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "unsetpc: bash sets POSIXLY_CORRECT=y in POSIX mode, and unsetting it turns POSIX mode off" {
  run_lab "$PROBE" diff unsetpc
  case $(lab_shell_family) in
    bash) want='default=y,unset/0 posix=y,unset/0' ;;
    yash | busybox | mksh) want='native=unset,1/0' ;;
    ksh93)
      case $(series) in
        93u+m) want='native=unset,1/0' ;;
        93u+2012) want='native=/2' ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | posh) want='native=/1' ;;
    dash) want='native=/2' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "pcenv: bash does not export the POSIXLY_CORRECT it sets, so child processes never see it" {
  run_lab "$PROBE" diff pcenv
  case $(lab_shell_family) in
    bash) want='default=absent/0 posix=absent/0' ;;
    yash | busybox | mksh) want='native=absent/0' ;;
    ksh93)
      case $(series) in
        93u+m) want='native=absent/0' ;;
        93u+2012) want='native=/2' ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | posh) want='native=/1' ;;
    dash) want='native=/2' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "dot: from bash 4.4, POSIX mode stops . from falling back to the current directory, fatally" {
  local curdir=unmeasured family
  family=$(lab_shell_family)
  if [ "$family" = busybox ]; then
    # BusyBox decides at build time (ASH_BASH_SOURCE_CURDIR): Alpine enables the fallback,
    # Debian does not. Detect the build with a separate run, so the probe's result below is
    # checked against an expectation that does not come from the probe's own output.
    mkdir "$BATS_TEST_TMPDIR/cur"
    printf 'echo sourced\n' >"$BATS_TEST_TMPDIR/cur/lib.sh"
    # shellcheck disable=SC2016 # $1 belongs to the shell under test
    run_lab_c 'cd "$1" || exit 9
      PATH=/nonexistent
      . lib.sh 2>/dev/null' probe "$BATS_TEST_TMPDIR/cur"
    case $status:$output in
      0:sourced) curdir=yes ;;
      2:) curdir=no ;;
      *) false ;;
    esac
  fi
  run_lab "$PROBE" diff dot
  case $(lab_shell_family) in
    bash)
      case $(series) in
        3.2) want='default=yes/0 posix=yes/0' ;;
        4.4 | 5.1-5.2 | 5.3) want='default=yes/0 posix=/1' ;;
        *) false ;;
      esac
      ;;
    yash) want='native=yes/0' ;;
    zsh) want='native=no/0' ;; # not found, but not fatal in native zsh
    zsh-sh | posh | mksh | ksh93) want='native=/1' ;;
    dash) want='native=/2' ;;
    busybox)
      case $curdir in
        yes) want='native=yes/0' ;;
        no) want='native=/2' ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
  expect "$want"
}

@test "cmdstatus: bash 5.3 POSIX mode stops a command substitution from setting \$? mid-command" {
  run_lab "$PROBE" diff cmdstatus
  case $(lab_shell_family) in
    bash)
      case $(series) in
        3.2 | 4.4 | 5.1-5.2) want='default=3/0 posix=3/0' ;;
        5.3) want='default=3/0 posix=1/0' ;;
        *) false ;;
      esac
      ;;
    yash | busybox | posh | mksh | dash) want='native=1/0' ;;
    zsh | zsh-sh | ksh93) want='native=3/0' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "setfn: set with no arguments lists functions in default bash, only variables in POSIX mode" {
  run_lab "$PROBE" diff setfn
  case $(lab_shell_family) in
    bash) want='default=listed/0 posix=hidden/0' ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) want='native=hidden/0' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "funcname: a function named unset never runs in POSIX mode; bash 4.4 and later reject it fatally" {
  run_lab "$PROBE" diff funcname
  case $(lab_shell_family) in
    bash)
      case $(series) in
        3.2) want='default=fn,end/0 posix=end/0' ;;
        4.4 | 5.1-5.2 | 5.3) want='default=fn,end/0 posix=/2' ;;
        *) false ;;
      esac
      ;;
    yash | posh | mksh | ksh93) want='native=end/0' ;;
    zsh | zsh-sh | busybox) want='native=fn,end/0' ;;
    dash) want='native=/2' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "readonly: a readonly assignment error ends a POSIX-mode bash -c with status 127" {
  run_lab "$PROBE" diff readonly
  case $(lab_shell_family) in
    bash) want='default=survived/0 posix=/127' ;;
    yash | busybox | mksh | dash) want='native=/2' ;;
    zsh | zsh-sh | posh | ksh93) want='native=/1' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "badexport: only bash 5.3 POSIX mode exits on export of an invalid name" {
  run_lab "$PROBE" diff badexport
  case $(lab_shell_family) in
    bash)
      case $(series) in
        3.2 | 4.4 | 5.1-5.2) want='default=survived/0 posix=survived/0' ;;
        5.3) want='default=survived/0 posix=/1' ;;
        *) false ;;
      esac
      ;;
    yash) want='native=survived/0' ;;
    zsh | zsh-sh | posh | mksh | ksh93) want='native=/1' ;;
    busybox | dash) want='native=/2' ;;
    *) false ;;
  esac
  expect "$want"
}

@test "cmdexport: from bash 4.4, command export keeps declaration-utility expansion only in POSIX mode" {
  run_lab "$PROBE" diff cmdexport
  case $(lab_shell_family) in
    bash)
      case $(series) in
        3.2) want='default=[a]/0 posix=[a]/0' ;;
        4.4 | 5.1-5.2 | 5.3) want='default=[a]/0 posix=[a b]/0' ;;
        *) false ;;
      esac
      ;;
    yash | busybox | mksh | ksh93 | dash) want='native=[a b]/0' ;;
    zsh-sh | posh) want='native=[a]/0' ;;
    zsh) want='native=[]/0' ;; # native zsh's command runs only external commands
    *) false ;;
  esac
  expect "$want"
}

@test "usage: an unknown snippet or mode is a usage error with status 2" {
  run_lab "$PROBE" diff no-such-snippet
  [ "$status" -eq 2 ]
  [ "$output" = "posix-probe: unknown snippet: no-such-snippet" ]
  run_lab "$PROBE" no-such-mode
  [ "$status" -eq 2 ]
  [ "$output" = "usage: posix-probe.sh personality | diff NAME" ]
}

# ls_flavor: print the implementation behind ls as uutils, gnu, busybox, or bsd, or "unknown" so
# the test that branches on it fails for an implementation nobody has measured.
ls_flavor() {
  local version help
  version=$(ls --version 2>/dev/null) || version=""
  help=$(ls --help 2>&1) || true
  case $version:$help in
    *"uutils coreutils"*) echo uutils ;;
    *"GNU coreutils"*) echo gnu ;;
    *BusyBox*) echo busybox ;;
    *"usage: ls [-"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

@test "utility: an exported POSIXLY_CORRECT also stops GNU getopt from permuting, so GNU ls reads -d as a file" {
  mkdir "$BATS_TEST_TMPDIR/d"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'cd "$1" || exit 9
    ls d -d >/dev/null 2>&1
    a=$?
    POSIXLY_CORRECT=1 ls d -d >/dev/null 2>&1
    b=$?
    echo "plain=$a pc=$b"' probe "$BATS_TEST_TMPDIR"
  case $(ls_flavor) in
    gnu) want='plain=0 pc=2' ;;     # glibc getopt permutes unless POSIXLY_CORRECT is set
    uutils) want='plain=0 pc=0' ;;  # uutils ls 0.10.0 permutes either way
    busybox) want='plain=0 pc=0' ;; # BusyBox ls permutes either way
    bsd) want='plain=1 pc=1' ;;     # BSD getopt never permutes: "-d" is always an operand
    *) false ;;
  esac
  expect "$want"
}

@test "fnimport: POSIX mode still imports exported functions with valid names, refusing only names like pp-g" {
  lab_shell_words
  # The shell under test starts itself again with two exported bash functions in its environment,
  # the way sshd or a CGI server passes attacker-influenced variables to a shell.
  # shellcheck disable=SC2016 # "$@" belongs to the shell under test
  run_lab_c 'exec env "BASH_FUNC_pp_f%%=() { echo f-imported; }" "BASH_FUNC_pp-g%%=() { echo g-imported; }" "$@"' \
    probe "${LAB_SHELL_WORDS[@]}" "$PROBE" diff fnimport
  case $(lab_shell_family) in
    bash) want='default=f-imported,g-imported/0 posix=f-imported,no-g/0' ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) want='native=no-f,no-g/0' ;;
    *) false ;;
  esac
  expect "$want"
}
