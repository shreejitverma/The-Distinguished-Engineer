#!/usr/bin/env bats
# shellcheck disable=SC2030,SC2031 # each bats test runs in its own subshell, so its exports cannot leak
# What a shell script may assume about the file hierarchy: the POSIX minimum, getconf PATH and
# command -p, each shell's fallback PATH, merged /usr, XDG base directories, and FHS PID files.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/fhs_probe.sh"
}

@test "the POSIX minimum exists: /, /dev, /tmp, a character /dev/null that reads EOF, and /dev/tty" {
  run_lab "$PROBE" posix-files
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "root=dir dev=dir tmp=dir" ]
  [ "${lines[1]}" = "null=char null_read_bytes=0 null_write=ok" ]
  [ "${lines[2]}" = "tty=char" ]
}

@test "the two shebang anchors exist on every matrix host: /bin/sh and /usr/bin/env" {
  run_lab_c 'if [ -x /bin/sh ] && [ -x /usr/bin/env ]; then echo anchors=ok; else echo anchors=missing; fi'
  [ "$status" -eq 0 ]
  [ "$output" = "anchors=ok" ]
}

@test "a merged /usr makes /bin/sh and /usr/bin/sh one file; a split /usr keeps /bin and /usr/bin distinct" {
  run_lab "$PROBE" layout
  [ "$status" -eq 0 ]
  case $output in
    "bin=merged usr_bin_sh=same var_run="*) ;;
    "bin=split usr_bin_sh="*)
      # Independent check: the two directories have different inode numbers or devices.
      # shellcheck disable=SC2016 # the expansions belong to the shell under test
      run_lab_c 'ls -di /bin/. /usr/bin/. | { read -r a _; read -r b _; if [ "$a" != "$b" ]; then echo distinct; fi; }'
      [ "$output" = distinct ]
      ;;
    *) false ;;
  esac
}

@test "command -p getconf PATH yields absolute directories, and one of them holds an executable sh" {
  run_lab "$PROBE" standard-path
  [ "$status" -eq 0 ]
  std=${lines[0]#getconf_path=}
  shpath=${lines[1]#sh=}
  [ "${lines[0]}" != "$std" ]
  case $shpath in /*/sh) ;; *) false ;; esac
  [ -x "$shpath" ]
  case ":$std:" in *":${shpath%/sh}:"*) ;; *) false ;; esac
}

@test "command -p finds ls with a useless PATH; only bash then remembers it for a bare ls" {
  run_lab_c 'PATH=/nonexistent-dir
    if command -p ls -d / >/dev/null 2>&1; then echo p=ok; else echo p=fail; fi
    if ls -d / >/dev/null 2>&1; then echo after=ok; else echo after=fail; fi'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash) [ "$output" = "$(printf 'p=ok\nafter=ok')" ] ;; # execute_disk_command hashes command -p results
    dash | busybox | zsh | zsh-sh | mksh | posh | yash)
      [ "$output" = "$(printf 'p=ok\nafter=fail')" ]
      ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) [ "${lines[0]}" = p=ok ] ;;                   # the bare-ls line is not asserted: never run on ksh93u+m
        *) [ "$output" = "$(printf 'p=fail\nafter=fail')" ] ;; # fixed in ksh93u+m 2020-05-19
      esac
      ;;
    *) false ;;
  esac
}

@test "with no PATH in the environment, each shell falls back to its own compiled-in directory list" {
  lab_shell_words
  family=$(lab_shell_family)
  abs=$(command -v "${LAB_SHELL_WORDS[0]}")
  args=("$abs" "${LAB_SHELL_WORDS[@]:1}")
  # Native zsh reads /etc/zshenv even under env -i; -f keeps site files from rewriting PATH.
  [ "$family" != zsh ] || args+=(-f)
  run_lab "$PROBE" fallback-path "${args[@]}"
  [ "$status" -eq 0 ]
  found=$(printf 'search=found')
  case $family in
    bash)
      case $(lab_shell_version) in
        [123].* | 4.[012].*)
          [ "$output" = "$(printf 'path=/usr/gnu/bin:/usr/local/bin:/bin:/usr/bin:.\n%s' "$found")" ]
          ;;
        *)
          # Upstream DEFAULT_PATH_VALUE since 4.3, or nixpkgs, which compiles in /no-such-path.
          [ "$output" = "$(printf 'path=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:.\n%s' "$found")" ] \
            || [ "$output" = "$(printf 'path=/no-such-path\nsearch=notfound')" ]
          ;;
      esac
      ;;
    dash) [ "$output" = "$(printf 'path=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n%s' "$found")" ] ;;
    zsh | zsh-sh) [ "$output" = "$(printf 'path=/bin:/usr/bin:/usr/ucb:/usr/local/bin\n%s' "$found")" ] ;;
    busybox) [ "$output" = "$(printf 'path=/sbin:/usr/sbin:/bin:/usr/bin\n%s' "$found")" ] ;;
    mksh)
      # mksh uses <paths.h> _PATH_DEFPATH: glibc and macOS "/usr/bin:/bin", musl adds /usr/local/bin.
      os=$(uname -s)
      if [ "$os" = Darwin ] || getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
        [ "$output" = "$(printf 'path=/usr/bin:/bin\n%s' "$found")" ]
      else
        [ "$output" = "$(printf 'path=/usr/local/bin:/bin:/usr/bin\n%s' "$found")" ]
      fi
      ;;
    posh) [ "$output" = "$(printf 'path=/bin:/usr/bin\n%s' "$found")" ] ;; # confstr(_CS_PATH)
    yash) [ "$output" = "$(printf 'path=<unset>\nsearch=notfound')" ] ;;
    ksh93) [ "$output" = "$(printf 'path=<unset>\n%s' "$found")" ] ;; # searches getconf PATH
    *) false ;;
  esac
}

@test "a leading zero makes a PID octal in POSIX arithmetic, decimal in native zsh, ksh93 and mksh" {
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'n=0042; echo $((n))'
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | busybox | zsh-sh | posh | yash) [ "$output" = 34 ] ;;
    zsh | ksh93 | mksh) [ "$output" = 42 ] ;;
    *) false ;;
  esac
}

@test "a tolerant PID-file reader strips blanks, leading zeroes, CR and extra lines without arithmetic" {
  d=$BATS_TEST_TMPDIR
  printf '25\n' >"$d/a.pid"
  printf '  0042  \n7\n' >"$d/b.pid"
  printf '25' >"$d/c.pid"
  printf '0\n' >"$d/d.pid"
  printf '\t1230\r\n' >"$d/e.pid"
  printf 'abc\n' >"$d/f.pid"
  run_lab "$PROBE" pidfile "$d/a.pid"
  [ "$output" = pid=25 ]
  run_lab "$PROBE" pidfile "$d/b.pid"
  [ "$output" = pid=42 ]
  run_lab "$PROBE" pidfile "$d/c.pid"
  [ "$output" = pid=25 ]
  run_lab "$PROBE" pidfile "$d/d.pid"
  [ "$output" = pid=0 ]
  run_lab "$PROBE" pidfile "$d/e.pid"
  [ "$output" = pid=1230 ]
  run_lab "$PROBE" pidfile "$d/f.pid"
  [ "$status" -eq 2 ]
}

@test "an HDB UUCP lock file is eleven bytes: a right-aligned ten-byte PID and a newline" {
  f=$BATS_TEST_TMPDIR/LCK..ttyS0
  run_lab "$PROBE" hdb-lock 1230 "$f"
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'wc -c <"$1"' sh "$f"
  [ "${output// /}" = 11 ]
  # shellcheck disable=SC2016 # the expansion belongs to the shell under test
  run_lab_c 'cat "$1"' sh "$f"
  [ "$output" = "      1230" ]
  run_lab "$PROBE" pidfile "$f"
  [ "$output" = pid=1230 ]
}

@test "XDG defaults apply when every variable is unset" {
  export HOME="$BATS_TEST_TMPDIR/home"
  unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME XDG_CONFIG_DIRS XDG_DATA_DIRS XDG_RUNTIME_DIR
  run_lab "$PROBE" xdg
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "config_home=$HOME/.config" ]
  [ "${lines[1]}" = "data_home=$HOME/.local/share" ]
  [ "${lines[2]}" = "state_home=$HOME/.local/state" ]
  [ "${lines[3]}" = "cache_home=$HOME/.cache" ]
  [ "${lines[4]}" = "config_dirs=/etc/xdg" ]
  [ "${lines[5]}" = "data_dirs=/usr/local/share/:/usr/share/" ]
  [ "${lines[6]}" = "runtime_dir=unset" ]
}

@test "XDG ignores empty and relative values, entry by entry in the colon lists" {
  export HOME="$BATS_TEST_TMPDIR/home"
  export XDG_CONFIG_HOME=relative/cfg XDG_DATA_HOME='' XDG_STATE_HOME=/abs/state XDG_CACHE_HOME=.cache
  export XDG_CONFIG_DIRS=':rel:/opt/a::/opt/b:' XDG_DATA_DIRS='only/relative'
  unset XDG_RUNTIME_DIR
  run_lab "$PROBE" xdg
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "config_home=$HOME/.config" ]
  [ "${lines[1]}" = "data_home=$HOME/.local/share" ]
  [ "${lines[2]}" = "state_home=/abs/state" ]
  [ "${lines[3]}" = "cache_home=$HOME/.cache" ]
  [ "${lines[4]}" = "config_dirs=/opt/a:/opt/b" ]
  [ "${lines[5]}" = "data_dirs=/usr/local/share/:/usr/share/" ]
}

@test "XDG_RUNTIME_DIR passes only as a directory the user owns with mode 0700" {
  export HOME="$BATS_TEST_TMPDIR/home"
  rt="$BATS_TEST_TMPDIR/rt"
  mkdir "$rt"
  chmod 700 "$rt"
  export XDG_RUNTIME_DIR="$rt"
  run_lab "$PROBE" xdg
  [ "${lines[6]}" = "runtime_dir=$rt ok" ]
  chmod 755 "$rt"
  run_lab "$PROBE" xdg
  [ "${lines[6]}" = "runtime_dir=$rt insecure" ]
  export XDG_RUNTIME_DIR="$BATS_TEST_TMPDIR/absent"
  run_lab "$PROBE" xdg
  [ "${lines[6]}" = "runtime_dir=$XDG_RUNTIME_DIR missing" ]
}

@test "xdg-find returns the user file first, then the first system directory, else none" {
  t=$BATS_TEST_TMPDIR
  export HOME="$t/home"
  unset XDG_CONFIG_HOME
  export XDG_CONFIG_DIRS="$t/sys1:$t/sys2"
  mkdir -p "$HOME/.config" "$t/sys1" "$t/sys2"
  printf 'user\n' >"$HOME/.config/app.conf"
  printf 'one\n' >"$t/sys1/app.conf"
  printf 'two\n' >"$t/sys2/app.conf"
  run_lab "$PROBE" xdg-find app.conf
  [ "$status" -eq 0 ]
  [ "$output" = "$HOME/.config/app.conf" ]
  rm "$HOME/.config/app.conf"
  run_lab "$PROBE" xdg-find app.conf
  [ "$output" = "$t/sys1/app.conf" ]
  rm "$t/sys1/app.conf" "$t/sys2/app.conf"
  run_lab "$PROBE" xdg-find app.conf
  [ "$status" -eq 1 ]
  [ "$output" = none ]
}
