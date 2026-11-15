#!/bin/sh
# resolve-probe.sh: show how pathname resolution treats symbolic links, one mode at a time.
# Usage: resolve-probe.sh MODE DIR
#   DIR must be an existing empty directory; the probe builds its fixture there and never deletes it.
# Modes:
#   logical   cd and pwd: logical (string) versus physical (kernel) handling of dot-dot
#   fallback  cd l/../x where x exists only beside the link's target, not beside the link
#   relative  a relative symlink is resolved against the link's own directory
#   chain     how many chained symlinks the kernel follows before it gives up
#   slash     a trailing slash forces the final symlink to be followed
#   dangling  > through a dangling symlink creates the file the link names
#   toctou    check-then-write loses to a swap in the window; write-then-rename does not
#   mvdir     mv onto a symlink to a directory moves the file into that directory
#   pin       a descriptor opened on a directory still reaches it after a rename (via /dev/fd)
set -u

usage='usage: resolve-probe.sh MODE DIR'
mode=${1:?$usage}
dir=${2:?$usage}
base=$(cd -P -- "$dir" && pwd -P) || exit 2
cd -P -- "$base" || exit 2

# rel PATH: print PATH with the fixture directory removed, so output does not depend on where DIR is.
rel() {
  r=${1#"$base"}
  printf '%s\n' "${r:-/}"
}

# yn TEST-ARGS...: print y if test(1) succeeds on the arguments, else n.
yn() {
  if test "$@"; then echo y; else echo n; fi
}

case $mode in
  logical)
    mkdir -p a/b || exit 2
    ln -s a/b l || exit 2
    cd l || exit 2
    lpwd=$(pwd)
    ppwd=$(pwd -P)
    cd .. || exit 2
    up=$(pwd)
    cd -P -- "$base" || exit 2
    cd -P l/.. || exit 2
    pup=$(pwd)
    lpwd=$(rel "$lpwd")
    ppwd=$(rel "$ppwd")
    up=$(rel "$up")
    pup=$(rel "$pup")
    printf 'pwd=%s pwd-P=%s cd-..=%s cd-P-l/..=%s\n' "$lpwd" "$ppwd" "$up" "$pup"
    ;;
  fallback)
    mkdir -p a/b a/x || exit 2
    ln -s a/b l || exit 2
    if cd l/../x 2>/dev/null; then
      here=$(pwd -P) || exit 2
      rel "$here"
    else
      echo FAIL
    fi
    ;;
  relative)
    mkdir sub || exit 2
    printf 'top\n' >target || exit 2
    printf 'sub\n' >sub/target || exit 2
    ln -s target sub/link || exit 2
    contents=$(readlink sub/link) || exit 2
    reached=$(cat sub/link) || exit 2
    printf 'readlink=%s cat=%s\n' "$contents" "$reached"
    ;;
  chain)
    # f0 is a file; fN is a symlink to f(N-1), so opening fN follows exactly N links.
    printf 'payload\n' >f0 || exit 2
    i=1
    while [ "$i" -le 64 ]; do
      ln -s "f$((i - 1))" "f$i" || exit 2
      i=$((i + 1))
    done
    n=1
    # /bin/cat, not cat: the open must happen in a separate process, never a shell builtin.
    while [ "$n" -le 64 ] && /bin/cat "f$n" >/dev/null 2>&1; do
      n=$((n + 1))
    done
    printf 'followed=%d\n' "$((n - 1))"
    ;;
  slash)
    mkdir d || exit 2
    ln -s d l || exit 2
    ln -s missing dang || exit 2
    hl=$(yn -h l)
    hls=$(yn -h l/)
    dls=$(yn -d l/)
    hd=$(yn -h dang)
    ed=$(yn -e dang)
    printf 'h(l)=%s h(l/)=%s d(l/)=%s h(dang)=%s e(dang)=%s\n' "$hl" "$hls" "$dls" "$hd" "$ed"
    ;;
  dangling)
    ln -s victim dang || exit 2
    printf 'data\n' >dang || exit 2
    victim=$(cat victim) || exit 2
    still=$(yn -h dang)
    printf 'victim=%s dang-still-link=%s\n' "$victim" "$still"
    ;;
  toctou)
    # Unsafe: check, then use. The attacker's swap is simulated inside the window, so the
    # outcome is deterministic; a real attacker loops until the scheduler gives the same order.
    printf 'original\n' >protected || exit 2
    if [ ! -h out ]; then
      ln -s protected out || exit 2 # attacker: plant the link after the check
      printf 'clobbered\n' >out || exit 2
    fi
    unsafe=$(cat protected)
    # Safer: write a private file, then rename it over the name; rename replaces the link itself.
    printf 'original\n' >protected || exit 2
    priv=$(mktemp -d "$base/w.XXXXXX") || exit 2
    printf 'fresh\n' >"$priv/new" || exit 2
    mv -f "$priv/new" out || exit 2
    safe=$(cat protected) || exit 2
    islink=$(yn -h out)
    now=$(cat out) || exit 2
    printf 'unsafe: protected=%s\nrename: protected=%s out-is-link=%s out=%s\n' \
      "$unsafe" "$safe" "$islink" "$now"
    ;;
  mvdir)
    mkdir realdir || exit 2
    ln -s realdir dl || exit 2
    printf 'x\n' >newfile || exit 2
    mv newfile dl || exit 2
    islink=$(yn -h dl)
    inside=$(ls realdir) || exit 2
    printf 'dl-is-link=%s realdir=%s\n' "$islink" "$inside"
    ;;
  pin)
    mkdir old || exit 2
    printf 'pinned\n' >old/f || exit 2
    exec 3<old || exit 2
    mv old new || exit 2
    # 3<&3 passes the descriptor on explicitly: ksh93 and mksh close exec-opened fds above 2 on exec.
    if out=$(/bin/cat /dev/fd/3/f 3<&3 2>/dev/null); then :; else out=FAIL; fi
    exec 3<&-
    gone=$(yn -e old)
    printf 'old-exists=%s via-fd=%s\n' "$gone" "$out"
    ;;
  *)
    printf '%s\n' "$usage" >&2
    exit 2
    ;;
esac
