#!/bin/sh
# awprobe.sh MODE: contrast a plain "> file" redirection with atomic_write.
# Each mode works in a fresh directory from mktemp -d under TMPDIR and prints one
# line of key=value results; awprobe.bats asserts them per shell and per userland.

# shellcheck disable=SC2312 # report lines cat files the probe just wrote; a failed cat
# prints a wrong value, which the exact-match assertions in awprobe.bats catch.

# shellcheck source=SCRIPTDIR/atomic_write.sh
. "${0%/*}/atomic_write.sh"

# size FILE: print FILE's size in bytes without wc's padding.
size() {
  sz=$(wc -c <"$1") || return 1
  printf '%s\n' "$sz" | tr -d ' \t'
}

# mode_of FILE: print the ten-character mode column of ls -l for FILE.
mode_of() {
  mo=$(ls -ld -- "$1") || return 1
  mo=${mo%% *}
  mo_rest=${mo#??????????}
  printf '%s\n' "${mo%"$mo_rest"}"
}

# count_tmp: print how many .aw.* temporary files the current directory holds.
# find, not a glob: native zsh aborts on a glob that matches nothing.
count_tmp() {
  ct=$(find . ! -name . -prune -name ".aw.*" -print) || return 1
  if [ -z "$ct" ]; then echo 0; else printf "%s\n" "$ct" | wc -l | tr -d " \t"; fi
}

# leftovers: print how many .aw.* temporary files are left here, or none.
leftovers() {
  lo=$(count_tmp) || return 1
  if [ "$lo" = 0 ]; then echo none; else echo "$lo"; fi
}

# joined FILE: print FILE's lines joined with commas.
joined() {
  tr '\n' , <"$1"
}

work=$(mktemp -d "${TMPDIR:-/tmp}/awprobe.XXXXXX") || exit 1
cd "$work" || exit 1

case ${1-} in
  truncate-first)
    # The shell opens and truncates f before sort runs, so sort reads nothing.
    printf 'b\na\n' >f
    # shellcheck disable=SC2094 # reading and writing f in one command is the point
    sort f >f
    naive=$(size f)
    printf 'b\na\n' >f
    atomic_write f sort f
    echo "naive_bytes=$naive atomic=$(joined f)"
    ;;
  reader-midwrite)
    # The producer looks at the destination halfway through its own output, as a
    # concurrent reader would, and counts the files that "*" and ".aw.*" match.
    producer() {
      printf 'new-1\n'
      seen=$(cat dest)
      set -- *
      globbed=$*
      hidden=$(count_tmp)
      printf 'new-2\n'
    }
    printf 'OLD\n' >dest
    producer >dest
    naive_seen=$seen
    printf 'OLD\n' >dest
    atomic_write dest producer
    echo "naive_seen=$naive_seen atomic_seen=$seen glob=$globbed hidden=$hidden final=$(joined dest)"
    ;;
  producer-fails)
    failing() {
      printf 'partial\n'
      return 3
    }
    printf 'OLD\n' >dest
    failing >dest
    naive_rc=$?
    naive=$(cat dest)
    printf 'OLD\n' >dest
    atomic_write dest failing
    rc=$?
    echo "naive_rc=$naive_rc naive=$naive atomic_rc=$rc atomic=$(cat dest) leftovers=$(leftovers)"
    ;;
  file-size-limit)
    # RLIMIT_FSIZE stands in for a full disk: with SIGXFSZ ignored, the write past the
    # limit fails with EFBIG and cat exits nonzero after a partial write.
    capped_cat() {
      (
        trap '' XFSZ
        ulimit -f 1
        exec /bin/cat -- "$1"
      )
    }
    awk 'BEGIN { for (i = 0; i < 2000; i++) print "line " i }' >payload
    printf 'OLD\n' >dest
    capped_cat payload >dest 2>/dev/null
    naive_rc=$?
    naive=$(size dest)
    printf 'OLD\n' >dest
    atomic_write dest capped_cat payload 2>/dev/null
    rc=$?
    echo "payload=$(size payload) naive_rc=$naive_rc naive_bytes=$naive atomic_rc=$rc atomic=$(cat dest) leftovers=$(leftovers)"
    ;;
  modes)
    # rename(2) installs the temporary file's inode, so its mode is what readers get.
    umask 022
    printf 'OLD\n' >kept
    chmod 640 kept
    atomic_write kept printf 'NEW\n'
    umask 002
    atomic_write fresh printf 'NEW\n'
    umask 022
    atomic_write -m 600 explicit printf 'NEW\n'
    printf 'OLD\n' >raw
    chmod 644 raw
    raw_tmp=$(mktemp ./.raw.XXXXXX) || exit 1
    printf 'NEW\n' >"$raw_tmp"
    mv -f -- "$raw_tmp" raw
    echo "kept=$(mode_of kept) fresh=$(mode_of fresh) explicit=$(mode_of explicit) mktemp_mv=$(mode_of raw)"
    ;;
  links)
    # A new inode: an open descriptor and a hard link both keep the old content.
    printf 'OLD\n' >dest
    ln dest hard
    exec 3<dest
    atomic_write dest printf 'NEW\n'
    fd=$(cat <&3)
    exec 3<&-
    echo "name=$(cat dest) fd=$fd hardlink=$(cat hard)"
    ;;
  symlink)
    # atomic_write refuses a symlink; a bare temp-and-mv replaces the link itself.
    printf 'OLD\n' >target
    ln -s target link
    atomic_write link printf 'NEW\n' 2>/dev/null
    rc=$?
    if [ -L link ]; then still=yes; else still=no; fi
    raw_tmp=$(mktemp ./.raw.XXXXXX) || exit 1
    printf 'NEW\n' >"$raw_tmp"
    mv -f -- "$raw_tmp" link
    if [ -L link ]; then raw_link=yes; else raw_link=no; fi
    echo "refuse_rc=$rc link=$still target=$(cat target) raw_link=$raw_link raw_name=$(cat link) raw_target=$(cat target)"
    ;;
  name-max)
    # A 250-byte name fits NAME_MAX (255); ".NAME.XXXXXX" (258 bytes) does not.
    long=$(awk 'BEGIN { s = ""; for (i = 0; i < 250; i++) s = s "a"; print s }')
    printf 'OLD\n' >"$long"
    if derived_tmp=$(mktemp "./.$long.XXXXXX" 2>/dev/null); then
      derived=ok
      rm -f -- "$derived_tmp"
    else
      derived=fail
    fi
    atomic_write "$long" printf 'NEW\n'
    rc=$?
    echo "name_bytes=${#long} derived=$derived aw_rc=$rc content=$(cat "$long")"
    ;;
  readonly-dir)
    # Replacing needs write permission on the directory; truncating needs it on the file.
    mkdir d
    printf 'OLD\n' >d/f
    chmod 555 d
    atomic_write d/f printf 'NEW\n' 2>/dev/null
    rc=$?
    after_atomic=$(cat d/f)
    printf 'NAIVE\n' >d/f
    naive_rc=$?
    chmod 755 d
    echo "atomic_rc=$rc after_atomic=$after_atomic naive_rc=$naive_rc after_naive=$(cat d/f)"
    ;;
  sigkill)
    # The shell dies by SIGKILL halfway through the producer; no trap can run.
    echo "work=$work"
    suicide() {
      printf 'partial\n'
      kill -KILL "$$"
    }
    printf 'OLD\n' >dest
    atomic_write dest suicide
    echo "not reached"
    ;;
  create-once)
    atomic_create dest printf 'FIRST\n'
    r1=$?
    atomic_create dest printf 'SECOND\n'
    r2=$?
    echo "first_rc=$r1 second_rc=$r2 dest=$(cat dest) leftovers=$(leftovers)"
    ;;
  durable)
    printf 'OLD\n' >dest
    # shellcheck disable=SC2034 # read by atomic_write in the sourced library
    AW_SYNC=1
    atomic_write dest printf 'NEW\n'
    rc=$?
    unset AW_SYNC
    printf 'keep\n' >other
    aw_flush other
    flush_rc=$?
    echo "rc=$rc dest=$(cat dest) leftovers=$(leftovers) flush_rc=$flush_rc other=$(cat other)"
    ;;
  *)
    echo "usage: awprobe.sh MODE" >&2
    exit 2
    ;;
esac
cd / && rm -rf -- "$work"
