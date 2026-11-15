#!/bin/sh
# installsem.sh MODE [ARG...]: probe one install(1), ln, or mv behavior that matters for
# deployment, in a fresh scratch directory, and print what happened as key=value pairs.
# The scratch directory comes from mktemp -d under TMPDIR (the bats file sets it to
# BATS_TEST_TMPDIR); the probe never removes anything it did not create.
# Utility diagnostics go to /dev/null so the output holds only the computed values.
# Usage: installsem.sh MODE [ARG...]   (modes are listed in the case statement at the bottom)
# shellcheck disable=SC2312 # every report line prints computed values; a failed read shows up as a wrong value the tests catch

umask 022
work=$(mktemp -d "${TMPDIR:-/tmp}/installsem.XXXXXX") || exit 2
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

# yes_if_equal A B: print yes when the two strings are equal, else no.
yes_if_equal() {
  if [ "$1" = "$2" ]; then echo yes; else echo no; fi
}

# is_link FILE: print yes if FILE is a symbolic link, else no.
is_link() {
  if [ -L "$1" ]; then echo yes; else echo no; fi
}

# replace: install over a file that has a second hard link and an open reader.
probe_replace() {
  printf 'old\n' >dst
  ln dst alias
  printf 'new\n' >src
  before=$(inode dst)
  exec 3<dst
  install src dst 2>/dev/null
  rc=$?
  fd=$(cat <&3)
  exec 3<&-
  echo "rc=$rc fd=$fd link=$(cat alias) name=$(cat dst)" \
    "same_inode=$(yes_if_equal "$before" "$(inode dst)") mode=$(mode_of dst)"
}

# umask: the installed mode ignores the umask, by default and with -m.
probe_umask() {
  printf 'x\n' >src
  (umask 077 && install src default 2>/dev/null)
  (umask 000 && install -m 600 src private 2>/dev/null)
  echo "default=$(mode_of default) m600=$(mode_of private)"
}

# symlink-dest: the destination is a symlink to another file.
probe_symlink_dest() {
  printf 'victim\n' >victim
  ln -s victim dst
  printf 'new\n' >src
  install src dst 2>/dev/null
  echo "rc=$? dst_is_link=$(is_link dst) victim=$(cat victim) dst=$(cat dst)"
}

# compare: -C on an identical and on a changed destination.
probe_compare() {
  printf 'same\n' >src
  install -m 644 src dst 2>/dev/null
  first=$(inode dst)
  install -C -m 644 src dst 2>/dev/null
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "rc=$rc"
    return
  fi
  second=$(inode dst)
  printf 'changed\n' >src
  install -C -m 644 src dst 2>/dev/null
  echo "rc=$rc identical_kept=$(yes_if_equal "$first" "$second")" \
    "changed_replaced=$(yes_if_equal no "$(yes_if_equal "$second" "$(inode dst)")")" \
    "content=$(cat dst)"
}

# mkparents: -D followed by SOURCE DEST with missing parents.
probe_mkparents() {
  printf 'x\n' >src
  install -D src a/b/c >/dev/null 2>&1
  rc=$?
  if [ -f a/b/c ]; then created=yes; else created=no; fi
  echo "rc=$rc created=$created"
}

# backup: -b keeps the old destination under some name; report which one.
probe_backup() {
  printf 'old\n' >dst
  printf 'new\n' >src
  install -b src dst >/dev/null 2>&1
  rc=$?
  backup=none
  for name in 'dst~' dst.old; do
    if [ -f "$name" ]; then backup="$name content=$(cat "$name")"; fi
  done
  echo "rc=$rc dst=$(cat dst) backup=$backup"
}

# dirmode: install -d -m 700 creates the leaf with exactly that mode.
probe_dirmode() {
  install -d -m 700 p/q 2>/dev/null
  echo "rc=$? leaf=$(mode_of p/q)"
}

# running APP NEWAPP TOOL: replace a running executable APP with NEWAPP using TOOL
# (install or cp). APP creates its ready file, sleeps 2 seconds, and exits 7;
# NEWAPP exits 9 at once. Report TOOL's status, the old process's exit status, and
# what the name runs afterwards.
probe_running() {
  cp "$1" app && cp "$2" newapp || return 2
  ./app ready 2 &
  pid=$!
  tries=100
  until [ -f ready ]; do
    tries=$((tries - 1))
    if [ "$tries" -le 0 ]; then
      kill "$pid" 2>/dev/null
      wait "$pid"
      return 2
    fi
    sleep 0.1
  done
  "$3" newapp app 2>/dev/null
  rc=$?
  wait "$pid"
  old=$?
  ./app ready2 0
  echo "tool_rc=$rc old=$old now=$?"
}

# mmap MAPPROBE TOOL: map a 64 KiB file, let TOOL (install or cp) put a 1-byte file
# in its place, then report what the mapping shows at its first and last byte.
probe_mmap() {
  head -c 65536 /dev/zero | tr '\0' A >f
  printf 'B' >b
  "$1" f "$2" b f
}

# mv-into: plain mv of a new symlink onto a "current" symlink that points at a directory.
probe_mv_into() {
  mkdir -p releases/r1 releases/r2
  ln -s releases/r1 current
  ln -s releases/r2 next
  mv -f next current 2>/dev/null
  echo "rc=$? current=$(readlink current) nested=$(ls releases/r1)"
}

# ln-nest: ln -sf without -n follows a symlink to a directory; ln -sfn replaces it.
probe_ln_nest() {
  mkdir -p releases/r1 releases/r2
  ln -s releases/r1 current
  ln -sf releases/r2 current 2>/dev/null
  plain="current=$(readlink current) nested=$(ls releases/r1)"
  ln -sfn releases/r2 current 2>/dev/null
  echo "$plain then_n=$(readlink current)"
}

case ${1-} in
  replace) probe_replace ;;
  umask) probe_umask ;;
  symlink-dest) probe_symlink_dest ;;
  compare) probe_compare ;;
  mkparents) probe_mkparents ;;
  backup) probe_backup ;;
  dirmode) probe_dirmode ;;
  running)
    shift
    probe_running "$@"
    ;;
  mmap)
    shift
    probe_mmap "$@"
    ;;
  mv-into) probe_mv_into ;;
  ln-nest) probe_ln_nest ;;
  *)
    echo "usage: installsem.sh MODE [ARG...]" >&2
    exit 2
    ;;
esac
