#!/usr/bin/env bats
# The traps that turn rm and its relatives into data loss, each shown in a sandbox
# under $BATS_TEST_TMPDIR. Where the wrong behavior would reach outside the sandbox
# (an empty variable in front of /), the probe prints the operands instead of running rm.
# shellcheck disable=SC2016 # every single-quoted script is code for the shell under test

setup() {
  load ../lib/lab
  T=$BATS_TEST_TMPDIR
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

@test "\${dir:?} stops a script before the destructive command, with a shell-specific status" {
  run_lab "$BATS_TEST_DIRNAME/guard-demo.sh" ''
  [[ $output == *"refusing: empty dir"* ]]
  [[ $output != *"would run"* ]]
  [[ $output != *after* ]]
  case $(lab_shell_family) in
    dash | busybox | yash) [ "$status" -eq 2 ] ;;
    bash | zsh | zsh-sh | ksh93 | mksh | posh) [ "$status" -eq 1 ] ;;
    *) false ;;
  esac
  run_lab "$BATS_TEST_DIRNAME/guard-demo.sh" "$T/app"
  [ "$status" -eq 0 ]
  [ "$output" = "would run: rm -rf -- $T/app/cache
after" ]
}

@test "\${dir:?} under sh -c: bash reports 127, the others their script status" {
  run_lab_c 'dir=; : "${dir:?}"; echo after'
  [[ $output != *after* ]]
  case $(lab_shell_family) in
    bash) [ "$status" -eq 127 ] ;;
    dash | busybox | yash) [ "$status" -eq 2 ] ;;
    zsh | zsh-sh | ksh93 | mksh | posh) [ "$status" -eq 1 ] ;;
    *) false ;;
  esac
}

@test "set -u catches an unset variable but not an empty one" {
  run_lab_c 'set -u; dir=; printf "<%s>\n" "$dir/"'
  [ "$status" -eq 0 ]
  [ "$output" = "</>" ]
  run_lab_c 'set -u; unset dir; printf "<%s>\n" "$dir/"; echo after'
  [ "$status" -ne 0 ]
  [[ $output != *"</>"* ]]
  [[ $output != *after* ]]
}

@test "an empty variable in front of /* expands to the entries of the root directory" {
  run_lab_c 'dir=; set -- "$dir"/*; printf "%s\n" "$1"'
  [ "$status" -eq 0 ]
  [ "$output" != "/*" ]
  case $output in
    /?*) [ -e "$output" ] ;;
    *) false ;;
  esac
}

@test "an unquoted operand is split into two rm operands, except in native zsh" {
  mkdir "$T/w"
  : >"$T/w/a"
  : >"$T/w/b"
  : >"$T/w/a b"
  run_lab_c 'cd "$1" || exit 1; dir="a b"; rm -rf $dir' sh "$T/w"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    zsh)
      [ -e "$T/w/a" ] && [ -e "$T/w/b" ] && [ ! -e "$T/w/a b" ]
      ;;
    bash | zsh-sh | dash | busybox | yash | ksh93 | mksh | posh)
      [ ! -e "$T/w/a" ] && [ ! -e "$T/w/b" ] && [ -e "$T/w/a b" ]
      ;;
    *) false ;;
  esac
}

@test "a file named -r turns rm * into a recursive removal; rm -- * does not" {
  mkdir -p "$T/w/sub" "$T/v/sub"
  : >"$T/w/-r"
  : >"$T/w/sub/x"
  : >"$T/v/-r"
  : >"$T/v/sub/x"
  run_lab_c 'cd "$1" || exit 1; rm *' sh "$T/w"
  [ "$status" -eq 0 ]
  [ ! -e "$T/w/sub" ]
  [ -e "$T/w/-r" ]
  run_lab_c 'cd "$1" || exit 1; rm -- *' sh "$T/v"
  [ "$status" -ne 0 ]
  [ -f "$T/v/sub/x" ]
  [ ! -e "$T/v/-r" ]
}

@test ".* includes . and .. in some shells" {
  mkdir "$T/g"
  : >"$T/g/.hidden"
  run_lab_c 'cd "$1" || exit 1; for f in .*; do printf "<%s>" "$f"; done' sh "$T/g"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      if bash_at_least 5 2; then
        [ "$output" = "<.hidden>" ]
      else
        [ "$output" = "<.><..><.hidden>" ]
      fi
      ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) [ "$output" = "<.hidden>" ] ;;
        *) [ "$output" = "<.><..><.hidden>" ] ;;
      esac
      ;;
    dash | busybox | yash) [ "$output" = "<.><..><.hidden>" ] ;;
    zsh | zsh-sh | mksh | posh) [ "$output" = "<.hidden>" ] ;;
    *) false ;;
  esac
}

@test "rm refuses . and .. and leaves the directory intact" {
  mkdir -p "$T/p/c"
  : >"$T/p/c/f"
  run_lab_c 'cd "$1" || exit 1; rm -rf .' sh "$T/p/c"
  [ "$status" -ne 0 ]
  [ -f "$T/p/c/f" ]
  run_lab_c 'cd "$1" || exit 1; rm -rf ..' sh "$T/p/c"
  [ "$status" -ne 0 ]
  [ -f "$T/p/c/f" ]
}

@test "rm -f with no operands succeeds; plain rm with none fails" {
  run_lab_c 'rm -f'
  [ "$status" -eq 0 ]
  run_lab_c 'rm 2>/dev/null'
  [ "$status" -ne 0 ]
}

@test "find -delete before -name deletes everything; after -name it deletes only matches" {
  mkdir "$T/f1" "$T/f2"
  : >"$T/f1/keep.txt"
  : >"$T/f1/x.tmp"
  : >"$T/f2/keep.txt"
  : >"$T/f2/x.tmp"
  run_lab_c 'find "$1" -delete -name "*.tmp"' sh "$T/f1"
  [ "$status" -eq 0 ]
  [ ! -e "$T/f1/keep.txt" ]
  run_lab_c 'find "$1" -name "*.tmp" -delete' sh "$T/f2"
  [ "$status" -eq 0 ]
  [ -f "$T/f2/keep.txt" ]
  [ ! -e "$T/f2/x.tmp" ]
}

@test "rm -rf link/ follows the link: macOS removes the target, GNU on Linux empties it, BusyBox removes only the link" {
  mkdir "$T/victim"
  : >"$T/victim/keep"
  ln -s "$T/victim" "$T/link"
  run_lab_c 'rm -rf "$1/" 2>/dev/null' sh "$T/link"
  # Every arm exits 0: on Linux GNU rm -f treats the final ENOTDIR as a missing file.
  [ "$status" -eq 0 ]
  if rm --help 2>&1 | grep -q BusyBox; then
    # BusyBox rm strips the trailing slash before removing, so only the link goes.
    [ ! -h "$T/link" ]
    [ -f "$T/victim/keep" ]
  else
    [ -h "$T/link" ]
    [ ! -e "$T/victim/keep" ]
    case $(uname -s) in
      Darwin) [ ! -e "$T/victim" ] ;;
      Linux)
        [ -d "$T/victim" ]
        # Without -f, GNU rm reports the ENOTDIR from the final rmdir.
        mkdir -p "$T/victim"
        run_lab_c 'rm -r "$1/"' sh "$T/link"
        [ "$status" -ne 0 ]
        [[ $output == *"Not a directory"* ]]
        [ -d "$T/victim" ]
        ;;
      *) false ;;
    esac
  fi
}

@test "an unlinked file stays readable through an open descriptor" {
  run_lab_c 'printf "data\n" >"$1"; exec 3<"$1"; rm -- "$1"; [ ! -e "$1" ] && echo gone; /bin/cat <&3' sh "$T/f"
  [ "$status" -eq 0 ]
  [ "$output" = "gone
data" ]
}

@test "set -C refuses to truncate an existing file and >| overrides it" {
  printf 'old\n' >"$T/nc"
  run_lab_c 'set -C; { echo new >"$1"; } 2>/dev/null || echo refused; cat "$1"; echo forced >|"$1"; cat "$1"' sh "$T/nc"
  [ "$status" -eq 0 ]
  [ "$output" = "refused
old
forced" ]
}

@test "an unmatched glob is passed to rm literally, except that native zsh aborts the command" {
  mkdir "$T/e"
  run_lab_c 'cd "$1" || exit 1; for f in *.tmp; do printf "<%s>" "$f"; done; echo " after"' sh "$T/e"
  case $(lab_shell_family) in
    zsh)
      [ "$status" -eq 1 ]
      [[ $output == *"no matches found"* ]]
      [[ $output != *after* ]]
      ;;
    bash | zsh-sh | dash | busybox | yash | ksh93 | mksh | posh)
      [ "$status" -eq 0 ]
      [ "$output" = "<*.tmp> after" ]
      ;;
    *) false ;;
  esac
}
