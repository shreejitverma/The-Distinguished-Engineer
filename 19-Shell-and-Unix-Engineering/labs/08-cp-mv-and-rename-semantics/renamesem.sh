#!/bin/sh
# renamesem.sh MODE: probe one cp, mv, or rename behavior in a fresh scratch directory
# and print what happened as key=value pairs.
# The scratch directory comes from mktemp -d under TMPDIR (the bats file sets it to
# BATS_TEST_TMPDIR); the probe never removes anything it did not create.
# Utility diagnostics go to /dev/null so the output holds only the computed values.
# Usage: renamesem.sh MODE   (modes are listed in the case statement at the bottom)
# shellcheck disable=SC2312 # every report line prints computed values; a failed read shows up as a wrong value the tests catch

umask 022
work=$(mktemp -d "${TMPDIR:-/tmp}/renamesem.XXXXXX") || exit 2
cd "$work" || exit 2

# inode FILE: print the inode number of FILE itself (not of a symlink target).
inode() {
  # shellcheck disable=SC2012 # ls -i is the portable way to read an inode number; names here are plain ASCII
  ls -id "$1" | awk '{ print $1 }'
}

# mode_of FILE: print the ten-character type and permission string from ls -l.
mode_of() {
  # shellcheck disable=SC2012 # the ls -l mode string is the value under test
  ls -ld "$1" | cut -c1-10
}

# exists FILE: print yes if FILE exists or is a (possibly dangling) symlink, else no.
exists() {
  if [ -e "$1" ] || [ -L "$1" ]; then echo yes; else echo no; fi
}

# yes_if_equal A B: print yes when the two strings are equal, else no.
yes_if_equal() {
  if [ "$1" = "$2" ]; then echo yes; else echo no; fi
}

# listing DIR: print every path below DIR, sorted, on one line.
listing() {
  (cd "$1" && find . ! -name . -print) | LC_ALL=C sort \
    | awk 'NR > 1 { printf " " } { printf "%s", $0 } END { print "" }'
}

# atomic_write DEST: copy standard input to DEST through a temporary file in DEST's
# own directory, then rename it over DEST, so a reader sees the old or the new file,
# never a partial one. The temporary file gets mode 644 before the rename, because
# mktemp creates it 0600 and rename keeps the inode, mode and all.
atomic_write() {
  case $1 in
    */) return 1 ;; # a trailing slash cannot name a regular file
    */*) aw_dir=${1%/*} ;;
    *) aw_dir=. ;;
  esac
  aw_dir=${aw_dir:-/}               # "/name" has an empty directory part
  if [ -d "$1" ]; then return 1; fi # mv would move the file INTO a directory
  aw_tmp=$(mktemp "$aw_dir/.atomic.XXXXXX") || return 1
  if cat >"$aw_tmp" && chmod 644 "$aw_tmp" && mv -f "$aw_tmp" "$1"; then
    return 0
  fi
  rm -f "$aw_tmp"
  return 1
}

case ${1-} in
  mv-reader)
    # rename(2) swaps the name to another inode; an open reader keeps the old one.
    printf OLD >r
    printf NEW >r.new
    new_ino=$(inode r.new)
    exec 3<r
    mv r.new r
    rc=$?
    via_fd=$(cat <&3)
    exec 3<&-
    now_ino=$(inode r)
    echo "rc=$rc fd=$via_fd name=$(cat r) inode_is_new=$(yes_if_equal "$now_ino" "$new_ino")"
    ;;
  cp-reader)
    # cp onto an existing file: truncate in place (POSIX) or unlink and recreate.
    printf old >dst
    chmod 600 dst
    ln dst dst.link
    printf new >src
    chmod 644 src
    old_ino=$(inode dst)
    exec 3<dst
    cp src dst
    rc=$?
    via_fd=$(cat <&3)
    exec 3<&-
    now_ino=$(inode dst)
    echo "rc=$rc fd=$via_fd link=$(cat dst.link) same_inode=$(yes_if_equal "$now_ino" "$old_ino") mode=$(mode_of dst)"
    ;;
  umask)
    # A new copy gets the source mode minus the umask; cp -p keeps the source mode.
    umask 077
    printf u >src
    chmod 644 src
    cp src plain
    cp -p src kept
    echo "plain=$(mode_of plain) kept=$(mode_of kept)"
    ;;
  preserve-time)
    # cp -p copies the modification time; a plain cp stamps the copy with now.
    printf t >src
    touch -t 200001020304 src
    cp src plain
    cp -p src kept
    plain_newer=$(find plain -newer src -print)
    kept_newer=$(find kept -newer src -print)
    echo "plain_newer=${plain_newer:-none} kept_newer=${kept_newer:-none}"
    ;;
  same-file)
    printf x >a
    cp a a 2>/dev/null
    cp_rc=$?
    mv a a 2>/dev/null
    mv_rc=$?
    echo "cp_rc=$cp_rc mv_rc=$mv_rc content=$(cat a)"
    ;;
  hardlinks)
    # mv between two names of one inode: rename(2) itself does nothing and succeeds.
    printf h >h1
    ln h1 h2
    mv h1 h2 2>/dev/null
    rc=$?
    echo "rc=$rc h1=$(exists h1) h2=$(exists h2)"
    ;;
  cp-tree-slash)
    mkdir -p s/sub
    printf f >s/f
    mkdir into into_slash
    cp -R s into
    cp -R s/ into_slash
    cp -R s/ fresh
    echo "into=[$(listing into)]"
    echo "into_slash=[$(listing into_slash)]"
    echo "fresh=[$(listing fresh)]"
    ;;
  mv-twice)
    # The same command renames the first time and nests the second time.
    mkdir build
    printf 1 >build/f
    mv build out
    mkdir build
    printf 2 >build/f
    mv build out
    echo "out=[$(listing out)]"
    ;;
  mv-nonempty)
    mkdir -p x/q y/x/z
    mv x y 2>/dev/null
    rc=$?
    echo "rc=$rc x=$(exists x) y_x_q=$(exists y/x/q)"
    ;;
  mv-into-self)
    mkdir md
    mv md md/sub 2>/dev/null
    rc=$?
    echo "rc=$rc md=$(exists md) sub=$(exists md/sub)"
    ;;
  trailing-slash)
    printf t >t
    mv t nodir/ 2>/dev/null
    mv_rc=$?
    cp t nodir2/ 2>/dev/null
    cp_rc=$?
    echo "mv_rc=$mv_rc cp_rc=$cp_rc t=$(exists t) nodir=$(exists nodir) nodir2=$(exists nodir2)"
    ;;
  symlink-dest)
    # mv replaces a symlink destination; cp may write through it to its target.
    printf T >target
    ln -s target viamv
    ln -s target viacp
    printf M >m
    printf C >c
    mv m viamv
    mv_rc=$?
    cp c viacp
    cp_rc=$?
    mv_link=no
    if [ -L viamv ]; then mv_link=yes; fi
    cp_link=no
    if [ -L viacp ]; then cp_link=yes; fi
    echo "mv_rc=$mv_rc mv_link=$mv_link mv_content=$(cat viamv) cp_rc=$cp_rc cp_link=$cp_link target=$(cat target)"
    ;;
  dangling | dangling-posix)
    ln -s missing dang
    printf C >c
    if [ "$1" = dangling-posix ]; then
      POSIXLY_CORRECT=1 cp c dang 2>/dev/null
    else
      cp c dang 2>/dev/null
    fi
    rc=$?
    dang_link=no
    if [ -L dang ]; then dang_link=yes; fi
    missing=absent
    if [ -e missing ]; then missing=$(cat missing); fi
    echo "rc=$rc dang_link=$dang_link missing=$missing"
    ;;
  no-clobber)
    printf A >a
    printf B >b
    mv -n a b 2>/dev/null
    mv_rc=$?
    cp -n a b 2>/dev/null
    cp_rc=$?
    echo "mv_rc=$mv_rc cp_rc=$cp_rc b=$(cat b) a=$(exists a)"
    ;;
  exchange)
    printf 1 >e1
    printf 2 >e2
    mv --exchange e1 e2 >/dev/null 2>&1
    rc=$?
    echo "rc=$rc e1=$(cat e1) e2=$(cat e2)"
    ;;
  atomic-write)
    printf OLD >conf
    old_ino=$(inode conf)
    exec 3<conf
    printf NEW | atomic_write conf
    rc=$?
    via_fd=$(cat <&3)
    exec 3<&-
    now_ino=$(inode conf)
    leftovers=$(find . -name '.atomic.*' -print)
    echo "rc=$rc fd=$via_fd name=$(cat conf) same_inode=$(yes_if_equal "$now_ino" "$old_ino") mode=$(mode_of conf) leftovers=${leftovers:-none}"
    ;;
  atomic-write-fail)
    # A failed write (stdin closed) leaves the old file and no temporary file behind.
    printf OLD >conf
    atomic_write conf <&- 2>/dev/null
    rc=$?
    mkdir adir
    printf X | atomic_write adir
    dir_rc=$?
    leftovers=$(find . -name '.atomic.*' -print)
    echo "rc=$rc name=$(cat conf) dir_rc=$dir_rc adir_entries=[$(listing adir)] leftovers=${leftovers:-none}"
    ;;
  *)
    echo "usage: renamesem.sh MODE" >&2
    exit 2
    ;;
esac
