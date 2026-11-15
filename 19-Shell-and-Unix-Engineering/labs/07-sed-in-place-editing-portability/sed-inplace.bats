#!/usr/bin/env bats
# Tests for sed-inplace.sh: what `sed -i` does to inodes, links, modes, and argument parsing.
# The shell under test runs the probe; the sed implementation is whatever `sed` is in PATH,
# so every utility assertion branches on sed_flavor, never on lab_shell_family.
# BusyBox expectations come from editors/sed.c (main) and are confirmed by the matrix's Alpine image.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/sed-inplace.sh"
}

# sed_flavor: print gnu, bsd (FreeBSD and macOS), busybox, or unknown for the sed in PATH.
# BusyBox answers --version with "This is not GNU sed version 4.0", so test that string first.
sed_flavor() {
  local out
  out=$(sed --version 2>&1 </dev/null) || true
  case $out in
    *"not GNU sed"*) echo busybox ;;
    *"GNU sed"*) echo gnu ;;
    *"illegal option"*"usage: sed"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# probe MODE: run the probe under the shell under test in this test's scratch directory.
probe() {
  run_lab "$PROBE" "$1" "$BATS_TEST_TMPDIR"
}

@test "the sed in PATH is an implementation this lab knows" {
  flavor=$(sed_flavor)
  case $flavor in
    gnu | bsd | busybox) true ;;
    *) false ;;
  esac
}

@test "-i writes a new inode and the backup keeps the original inode" {
  probe inode
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd | busybox)
      [ "${lines[0]}" = "status=0" ]
      [ "${lines[1]}" = "f=b|" ]
      [ "${lines[2]}" = "bak=a|" ]
      [ "${lines[3]}" = "f_inode=new" ]
      [ "${lines[4]}" = "bak_inode=original" ]
      [ "${lines[5]}" = "entries=f f.bak" ]
      ;;
    *) false ;;
  esac
}

@test "-i breaks a hard link: the other name keeps the old bytes" {
  probe hardlink
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd | busybox)
      [ "$output" = "$(printf '%s\n' status=0 'f=b|' 'g=a|' nlink_f=1 nlink_g=2)" ]
      ;;
    *) false ;;
  esac
}

@test "-i on a symlink: GNU and BusyBox replace the link, BSD refuses" {
  probe symlink
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | busybox)
      [ "$output" = "$(printf '%s\n' status=0 link_kind=regular 'link=b|' 'target=a|' stderr=no)" ]
      ;;
    bsd)
      [ "$output" = "$(printf '%s\n' status=1 link_kind=symlink 'link=a|' 'target=a|' stderr=yes)" ]
      ;;
    *) false ;;
  esac
}

@test "--follow-symlinks edits the target in GNU and is rejected elsewhere" {
  probe follow
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu) [ "$output" = "$(printf '%s\n' status=0 link_kind=symlink 'target=b|')" ] ;;
    bsd | busybox) [ "$output" = "$(printf '%s\n' status=1 link_kind=symlink 'target=a|')" ] ;;
    *) false ;;
  esac
}

@test "sed -i -e: BSD makes a backup named f-e" {
  probe dash-e
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | busybox) [ "$output" = "$(printf '%s\n' status=0 'f=b|' 'entries=f')" ] ;;
    bsd) [ "$output" = "$(printf '%s\n' status=0 'f=b|' 'entries=f f-e')" ] ;;
    *) false ;;
  esac
}

@test "sed -i '' works only in BSD; GNU and BusyBox read '' as the script" {
  probe empty-arg
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu) [ "$output" = "$(printf '%s\n' status=2 'f=a|' 'entries=f' stderr=yes)" ] ;;
    busybox) [ "$output" = "$(printf '%s\n' status=1 'f=a|' 'entries=f' stderr=yes)" ] ;;
    bsd) [ "$output" = "$(printf '%s\n' status=0 'f=b|' 'entries=f' stderr=no)" ] ;;
    *) false ;;
  esac
}

@test "sed -i 'script' file: BSD takes the script as the suffix and fails" {
  probe bare
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | busybox) [ "$output" = "$(printf '%s\n' status=0 'f=b|' 'entries=f')" ] ;;
    bsd) [ "$output" = "$(printf '%s\n' status=1 'f=a|' 'entries=f')" ] ;;
    *) false ;;
  esac
}

@test "an attached suffix (-i.bak) is accepted by every implementation" {
  probe attached
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd | busybox) [ "$output" = "$(printf '%s\n' 'before_rm=f f.bak' status=0 'f=b|')" ] ;;
    *) false ;;
  esac
}

@test "-i with no file operand is refused (GNU exits 4, BSD and BusyBox 1)" {
  probe stdin
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu) [ "$output" = "$(printf '%s\n' status=4 stdout= stderr=yes)" ] ;;
    bsd | busybox) [ "$output" = "$(printf '%s\n' status=1 stdout= stderr=yes)" ] ;;
    *) false ;;
  esac
}

@test "-i keeps mode 600; redirect-and-mv gets the umask's 644" {
  probe perms
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd | busybox) [ "$output" = "$(printf '%s\n' sed_i=-rw------- redirect_mv=-rw-r--r--)" ] ;;
    *) false ;;
  esac
}

@test "-i replaces a mode-444 file in a writable directory" {
  probe readonly
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd | busybox) [ "$output" = "$(printf '%s\n' status=0 'f=b|' mode=-r--r--r--)" ] ;;
    *) false ;;
  esac
}

@test "-i cannot edit a writable file in a read-only directory and leaves no temp file" {
  probe rodir
  [ "$status" -eq 0 ]
  if [ "${lines[0]}" = "root=yes" ]; then
    # root ignores directory permissions, so the edit goes through.
    [ "${lines[1]}" = "status=0" ]
    [ "${lines[2]}" = "f=b|" ]
    return
  fi
  [ "${lines[0]}" = "root=no" ]
  case $(sed_flavor) in
    gnu) [ "${lines[1]}" = "status=4" ] ;;
    bsd | busybox) [ "${lines[1]}" = "status=1" ] ;;
    *) false ;;
  esac
  [ "${lines[2]}" = "f=a|" ]
  [ "${lines[3]}" = "entries_d=f" ]
}

@test "a descriptor opened before -i keeps writing to the old inode (the backup)" {
  probe held-fd
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd | busybox) [ "$output" = "$(printf '%s\n' 'f=b|' 'bak=a|late|')" ] ;;
    *) false ;;
  esac
}

@test "temp file plus cat-over keeps the inode and the hard link" {
  probe truncate
  [ "$status" -eq 0 ]
  [ "$output" = "$(printf '%s\n' status=0 f_inode=same 'g=b|')" ]
}

@test "a missing final newline stays missing" {
  probe noeol
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd | busybox) [ "$output" = "$(printf '%s\n' f=b bak=a)" ] ;;
    *) false ;;
  esac
}

@test "-n with -i and no p command empties the file" {
  probe quiet
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd | busybox) [ "$output" = "$(printf '%s\n' f= 'bak=a|b|')" ] ;;
    *) false ;;
  esac
}

@test "-i treats each file separately: \$ is each file's last line" {
  probe per-file
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd | busybox) [ "$output" = "$(printf '%s\n' status=0 'a=1|' 'b=3|')" ] ;;
    *) false ;;
  esac
}

@test "BSD -I spans files: \$ is only the last file's last line; others reject -I" {
  probe span
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    bsd) [ "$output" = "$(printf '%s\n' status=0 'a=1|2|' 'b=3|')" ] ;;
    gnu | busybox) [ "$output" = "$(printf '%s\n' status=1 'a=1|2|' 'b=3|4|')" ] ;;
    *) false ;;
  esac
}

@test "the hold space is cleared between files in GNU and BSD, carried over in BusyBox" {
  probe hold
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu | bsd) [ "$output" = "$(printf '%s\n' 'a=|a1|' 'b=|b1|')" ] ;;
    busybox) [ "$output" = "$(printf '%s\n' 'a=|a1|' 'b=a2|b1|')" ] ;;
    *) false ;;
  esac
}

@test "a * in the suffix names the backup path in GNU only; elsewhere the edit fails cleanly" {
  probe star
  [ "$status" -eq 0 ]
  case $(sed_flavor) in
    gnu) [ "$output" = "$(printf '%s\n' status=0 'f=b|' 'bak_f=a|' 'entries=bak f')" ] ;;
    bsd | busybox) [ "$output" = "$(printf '%s\n' status=1 'f=a|' 'bak_f=<none>' 'entries=bak f')" ] ;;
    *) false ;;
  esac
}

@test "an unknown mode is an error" {
  probe no-such-mode
  [ "$status" -eq 2 ]
}
