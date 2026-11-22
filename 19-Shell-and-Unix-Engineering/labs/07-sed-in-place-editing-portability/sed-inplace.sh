#!/bin/sh
# sed-inplace.sh: probe what `sed -i` really does to a file, one mode at a time.
# Usage: sed-inplace.sh MODE PARENT_DIR
# Each run works in a fresh mktemp -d directory under PARENT_DIR and removes only that directory.
# Output is key=value lines computed from the file system after the edit.
# File contents are printed with each newline shown as `|`, so "b|" is the two bytes "b\n".
# The `sed` that runs is whatever `sed` is first in PATH (GNU, BSD/macOS, BusyBox, ...);
# the shell running this script never changes which `sed` that is.

mode=${1:?usage: sed-inplace.sh MODE PARENT_DIR}
parent=${2:?usage: sed-inplace.sh MODE PARENT_DIR}
work=$(mktemp -d "$parent/sedip.XXXXXX") || exit 99
cd "$work" || exit 99
work=$(pwd)
# The files under test live in t/; sed's stderr goes to ../err so it never shows up in a listing.
mkdir t && cd t || exit 99

# show KEY FILE: print KEY=bytes of FILE on one line, newline -> `|`; "<none>" if FILE is absent.
show() {
  printf '%s=' "$1"
  if [ -e "$2" ]; then
    tr '\n' '|' <"$2"
    printf '\n'
  else
    printf '<none>\n'
  fi
}

# inode FILE: print FILE's inode number.
inode() {
  # shellcheck disable=SC2012 # ls -i is the portable way to read an inode number; names here are plain ASCII
  ls -id "$1" | awk '{ print $1 }'
}

# nlink KEY FILE: print KEY=hard-link count of FILE.
nlink() {
  printf '%s=' "$1"
  # shellcheck disable=SC2012 # the ls -l link count is the value under test; names here are plain ASCII
  ls -ld "$2" | awk '{ print $2 }'
}

# perms KEY FILE: print KEY=the ten-character mode string of FILE, for example -rw-------.
perms() {
  printf '%s=' "$1"
  # shellcheck disable=SC2012 # the ls -l mode string is the value under test
  ls -ld "$2" | cut -c1-10
}

# entries KEY DIR: print KEY=every name in DIR (dot files too), space separated, in ls order.
entries() {
  printf '%s=' "$1"
  # shellcheck disable=SC2012 # the listing of plain ASCII names is the value under test
  ls -A "$2" | paste -s -d ' ' -
}

# stderr_seen: print whether sed wrote a diagnostic.
stderr_seen() {
  if [ -s ../err ]; then echo 'stderr=yes'; else echo 'stderr=no'; fi
}

case $mode in
  inode)
    # Does -i rewrite the inode, and what does the backup hold?
    printf 'a\n' >f
    before=$(inode f)
    sed -i.bak 's/a/b/' f
    echo "status=$?"
    show f f
    show bak f.bak
    after=$(inode f)
    bak=$(inode f.bak)
    if [ "$after" = "$before" ]; then echo 'f_inode=same'; else echo 'f_inode=new'; fi
    if [ "$bak" = "$before" ]; then echo 'bak_inode=original'; else echo 'bak_inode=other'; fi
    entries entries .
    ;;
  hardlink)
    # A second name for the same inode keeps the old bytes: -i breaks hard links.
    printf 'a\n' >f
    ln f g
    sed -i.bak 's/a/b/' f
    echo "status=$?"
    show f f
    show g g
    nlink nlink_f f
    nlink nlink_g g
    ;;
  symlink)
    # -i on a symbolic link: replace the link, edit the target, or refuse?
    printf 'a\n' >target
    ln -s target link
    sed -i.bak 's/a/b/' link 2>../err
    echo "status=$?"
    if [ -L link ]; then echo 'link_kind=symlink'; else echo 'link_kind=regular'; fi
    show link link
    show target target
    stderr_seen
    ;;
  follow)
    # GNU --follow-symlinks edits the link's target and keeps the link.
    printf 'a\n' >target
    ln -s target link
    sed --follow-symlinks -i 's/a/b/' link 2>../err
    echo "status=$?"
    if [ -L link ]; then echo 'link_kind=symlink'; else echo 'link_kind=regular'; fi
    show target target
    ;;
  dash-e)
    # `-i -e`: is "-e" the backup suffix (BSD) or the next option (GNU, BusyBox)?
    printf 'a\n' >f
    sed -i -e 's/a/b/' f 2>../err
    echo "status=$?"
    show f f
    entries entries .
    ;;
  empty-arg)
    # `-i ''`: the BSD spelling of "no backup"; GNU and BusyBox read '' as the script.
    printf 'a\n' >f
    sed -i '' 's/a/b/' f 2>../err
    echo "status=$?"
    show f f
    entries entries .
    stderr_seen
    ;;
  bare)
    # The GNU habit `sed -i 'script' file`: BSD takes the script as the suffix and the file name as the script.
    printf 'a\n' >f
    sed -i 's/a/b/' f 2>../err
    echo "status=$?"
    show f f
    entries entries .
    ;;
  attached)
    # `-i.bak` with the suffix attached is the one spelling all three accept.
    printf 'a\n' >f
    sed -i.bak 's/a/b/' f
    st=$?
    entries before_rm .
    rm -f f.bak
    echo "status=$st"
    show f f
    ;;
  stdin)
    # -i with no file operand has nothing to rename: every implementation must refuse.
    printf 'a\n' | sed -i 's/a/b/' >out 2>../err
    echo "status=$?"
    show stdout out
    stderr_seen
    ;;
  perms)
    # -i copies the mode bits; a naive redirect-and-mv takes the umask instead.
    umask 022
    printf 'a\n' >f
    chmod 600 f
    sed -i.bak 's/a/b/' f
    perms sed_i f
    printf 'a\n' >n
    chmod 600 n
    sed 's/a/b/' n >n.tmp && mv n.tmp n
    perms redirect_mv n
    ;;
  readonly)
    # A mode-444 file in a writable directory is still replaced: rename needs only directory write.
    printf 'a\n' >f
    chmod 444 f
    sed -i.bak 's/a/b/' f 2>../err
    echo "status=$?"
    show f f
    perms mode f
    ;;
  rodir)
    # A writable file in a read-only directory cannot be edited: there is nowhere to create the temp file.
    mkdir d
    printf 'a\n' >d/f
    chmod 555 d
    sed -i.bak 's/a/b/' d/f 2>../err
    st=$?
    chmod 755 d
    uid=$(id -u)
    if [ "$uid" = 0 ]; then echo 'root=yes'; else echo 'root=no'; fi
    echo "status=$st"
    show f d/f
    entries entries_d d
    ;;
  held-fd)
    # A writer that opened the file before the edit keeps writing to the old inode.
    printf 'a\n' >f
    exec 3>>f
    sed -i.bak 's/a/b/' f
    printf 'late\n' >&3
    exec 3>&-
    show f f
    show bak f.bak
    ;;
  truncate)
    # The inode-preserving alternative: write a temp file, then copy it back over the original.
    printf 'a\n' >f
    ln f g
    before=$(inode f)
    sed 's/a/b/' f >f.tmp && cat f.tmp >f && rm -f f.tmp
    echo "status=$?"
    after=$(inode f)
    if [ "$after" = "$before" ]; then echo 'f_inode=same'; else echo 'f_inode=new'; fi
    show g g
    ;;
  noeol)
    # A last line without a newline stays without one.
    printf 'a' >f
    sed -i.bak 's/a/b/' f
    show f f
    show bak f.bak
    ;;
  quiet)
    # -n with -i and no p command: the output, and so the file, is empty.
    printf 'a\nb\n' >f
    sed -n -i.bak 's/a/A/' f
    show f f
    show bak f.bak
    ;;
  per-file)
    # -i treats each file separately: `$` is the last line of each file.
    printf '1\n2\n' >a
    printf '3\n4\n' >b
    sed -i.bak '$d' a b
    echo "status=$?"
    show a a
    show b b
    ;;
  span)
    # BSD -I keeps one line-number space across files: `$` is the last line of the last file only.
    printf '1\n2\n' >a
    printf '3\n4\n' >b
    sed -I.bak '$d' a b 2>../err
    echo "status=$?"
    show a a
    show b b
    ;;
  hold)
    # Is the hold space cleared between files under -i? `x` prints the previous line of the same file.
    printf 'a1\na2\n' >a
    printf 'b1\nb2\n' >b
    sed -i.bak 'x' a b
    show a a
    show b b
    ;;
  star)
    # GNU expands `*` in the suffix to the file name, so `bak/*` puts the backup in a directory.
    mkdir bak
    printf 'a\n' >f
    sed -i'bak/*' 's/a/b/' f 2>../err
    echo "status=$?"
    show f f
    show bak_f bak/f
    entries entries .
    ;;
  *)
    echo "unknown mode: $mode" >&2
    cd / && rm -rf "$work"
    exit 2
    ;;
esac

cd / && rm -rf "$work"
