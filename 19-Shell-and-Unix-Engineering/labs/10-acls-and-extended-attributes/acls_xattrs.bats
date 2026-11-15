#!/usr/bin/env bats
# ACLs and extended attributes as a script sees them: the xattr system calls,
# their kernel limits, which rewrite idioms keep them, how test -w consults
# the ACL, how chmod and an ACL interact, and which cp copies xattrs by default.
# Kernel and utility facts branch on the kernel (uname -s) or the cp
# implementation; only the shell's own test builtin branches on the shell.

setup() {
  load ../lib/lab
  LAB_DIR="$BATS_TEST_DIRNAME"
  PROBE="$BATS_TEST_TMPDIR/xattr_probe"
  "${CC:-cc}" -Wall -Wextra -Werror -O2 -o "$PROBE" "$LAB_DIR/xattr_probe.c"
  KERNEL=$(uname -s)
  F="$BATS_TEST_TMPDIR/f"
  printf 'old\n' >"$F"
  chmod 644 "$F"
  ME=$(id -un)
  MYUID=$(id -u)
}

# cp_flavor: print gnu, uutils, busybox, or bsd for the cp on PATH.
cp_flavor() {
  local out path real
  out=$(cp --version 2>&1) || true
  case $out in
    *uutils*) echo uutils ;;
    *"GNU coreutils"*) echo gnu ;;
    *)
      path=$(command -v cp) || path="cp"
      real=$(readlink -f "$path" 2>/dev/null) || real=$path
      case $real in
        */busybox*) echo busybox ;;
        *) echo bsd ;;
      esac
      ;;
  esac
}

@test "a user xattr round-trips through setxattr, listxattr and getxattr" {
  run "$PROBE" set "$F" user.lab.note hello
  [ "$status" -eq 0 ]
  [ "$output" = ok ]
  run "$PROBE" list "$F" user.
  [ "$output" = user.lab.note ]
  run "$PROBE" get "$F" user.lab.note
  [ "$output" = hello ]
  run "$PROBE" get "$F" user.lab.absent
  [ "$status" -eq 1 ]
  case $KERNEL in
    Linux) [ "$output" = ENODATA ] ;;
    Darwin) [ "$output" = ENOATTR ] ;;
    *) false ;;
  esac
}

@test "a value one byte over 64 KiB is refused by the Linux VFS and accepted by APFS" {
  run "$PROBE" size "$F" user.lab.big 65537
  case $KERNEL in
    Linux)
      [ "$status" -eq 1 ]
      [ "$output" = E2BIG ]
      ;;
    Darwin)
      [ "$status" -eq 0 ]
      [ "$output" = ok ]
      ;;
    *) false ;;
  esac
}

@test "attribute names stop at 255 bytes on Linux and 127 bytes on Darwin" {
  case $KERNEL in
    Linux)
      run "$PROBE" namelen "$F" 255
      [ "$output" = ok ]
      run "$PROBE" namelen "$F" 256
      [ "$status" -eq 1 ]
      [ "$output" = ERANGE ]
      ;;
    Darwin)
      run "$PROBE" namelen "$F" 127
      [ "$output" = ok ]
      run "$PROBE" namelen "$F" 128
      [ "$status" -eq 1 ]
      [ "$output" = ENAMETOOLONG ]
      ;;
    *) false ;;
  esac
}

@test "truncating and appending redirections keep xattrs; write-then-rename drops them" {
  local mode
  for mode in truncate append rename; do
    "$PROBE" set "$F" user.lab.note kept >/dev/null
    run_lab "$LAB_DIR/rewrite.sh" "$mode" "$F" new
    [ "$status" -eq 0 ]
    run "$PROBE" get "$F" user.lab.note
    case $mode in
      truncate | append) [ "$output" = kept ] ;;
      rename)
        case $KERNEL in
          Linux) [ "$output" = ENODATA ] ;;
          Darwin) [ "$output" = ENOATTR ] ;;
          *) false ;;
        esac
        ;;
      *) false ;;
    esac
  done
}

@test "test -w asks the kernel, which applies the ACL before the mode bits" {
  case $KERNEL in
    Darwin)
      # Mode 644 says the owner may write; a deny ACE says otherwise.
      chmod +a "user:$ME deny write" "$F"
      ;;
    Linux)
      # user::r-- user:EUID:rw- group::r-- mask::rw- other::r--. The owner matches
      # ACL_USER_OBJ first, so the named entry for the same uid is never consulted.
      run "$PROBE" acl-set "$F" 4 6 4 6 4
      [ "$output" = ok ]
      ;;
    *) false ;;
  esac
  # shellcheck disable=SC2016 # $1 belongs to the shell under test
  run_lab_c 'if [ -w "$1" ]; then echo writable; else echo notwritable; fi' lab "$F"
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | dash | ksh93 | mksh | yash | posh) [ "$output" = notwritable ] ;;
    busybox)
      # BusyBox test computes -w from st_mode alone (coreutils/test.c, test_eaccess).
      case $KERNEL in
        Linux) [ "$output" = notwritable ] ;; # here the owner bits agree with the ACL
        Darwin) [ "$output" = writable ] ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "append and write are separate rights on Darwin; POSIX ACLs have only w" {
  case $KERNEL in
    Darwin)
      chmod +a "user:$ME deny write" "$F"
      run_lab "$LAB_DIR/rewrite.sh" append "$F" more
      [ "$status" -eq 0 ]
      run_lab "$LAB_DIR/rewrite.sh" truncate "$F" gone
      [ "$status" -ne 0 ]
      run cat "$F"
      [ "$output" = "old
more" ]
      ;;
    Linux)
      run "$PROBE" acl-set "$F" 4 6 4 6 4
      [ "$output" = ok ]
      run_lab "$LAB_DIR/rewrite.sh" append "$F" more
      [ "$status" -ne 0 ]
      run_lab "$LAB_DIR/rewrite.sh" truncate "$F" gone
      [ "$status" -ne 0 ]
      run cat "$F"
      [ "$output" = old ]
      ;;
    *) false ;;
  esac
}

@test "chmod g-w on a file with an extended ACL rewrites the mask (Linux) or leaves the ACE alone (Darwin)" {
  case $KERNEL in
    Linux)
      run "$PROBE" acl-set "$F" 6 6 4 6 4
      [ "$output" = ok ]
      # The group class bits of the mode now show the mask, not group::.
      run ls -l "$F"
      [ "${output:0:10}" = "-rw-rw-r--" ]
      chmod g-w "$F"
      run "$PROBE" acl-dump "$F"
      [ "$output" = "user::rw-
user:$MYUID:rw-
group::r--
mask::r--
other::r--" ]
      ;;
    Darwin)
      chmod +a "user:$ME allow write" "$F"
      chmod 600 "$F"
      run ls -le "$F"
      [[ "$output" == *"0: user:$ME allow write"* ]]
      chmod -N "$F"
      run ls -le "$F"
      [[ "$output" != *"allow write"* ]]
      ;;
    *) false ;;
  esac
}

@test "plain cp copies xattrs only in the BSD/macOS implementation; cp -a in all but BusyBox" {
  "$PROBE" set "$F" user.lab.note src >/dev/null
  cp "$F" "$BATS_TEST_TMPDIR/plain"
  cp -a "$F" "$BATS_TEST_TMPDIR/archive"
  run "$PROBE" list "$BATS_TEST_TMPDIR/plain" user.
  case $(cp_flavor) in
    gnu | uutils | busybox) [ "$output" = "" ] ;;
    bsd) [ "$output" = user.lab.note ] ;;
    *) false ;;
  esac
  run "$PROBE" list "$BATS_TEST_TMPDIR/archive" user.
  case $(cp_flavor) in
    gnu | uutils | bsd) [ "$output" = user.lab.note ] ;;
    busybox) [ "$output" = "" ] ;;
    *) false ;;
  esac
}
