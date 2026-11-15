#!/bin/sh
# tarlab.sh MODE: probe one tar behavior in a fresh scratch directory and print what
# happened as key=value pairs.
# The tar on PATH decides the behavior (GNU tar, bsdtar from libarchive, BusyBox tar);
# the shell under test only runs this script.
# Hostile archives come from mktar, a small C program whose path is in MKTAR.
# The scratch directory comes from mktemp -d under TMPDIR (the bats file sets it to
# BATS_TEST_TMPDIR); the probe never removes anything it did not create.
# Usage: tarlab.sh MODE   (modes are listed in the case statement at the bottom)
# shellcheck disable=SC2312 # every report line prints computed values; a failed read shows up as a wrong value the tests catch

umask 022
work=$(mktemp -d "${TMPDIR:-/tmp}/tarlab.XXXXXX") || exit 2
cd "$work" || exit 2
mkdir out outside || exit 2

# tar_flavor: print gnu, bsd, busybox, or unknown for the tar on PATH.
tar_flavor() {
  tf_out=$(tar --version 2>&1 </dev/null) || true
  case $tf_out in
    *"GNU tar"*) echo gnu ;;
    *bsdtar*) echo bsd ;;
    # BusyBox 1.37.0 prints "tar (busybox) 1.37.0" for --version.
    *BusyBox* | *busybox*) echo busybox ;;
    *) echo unknown ;;
  esac
}
flavor=$(tar_flavor)

# plain_c ARCHIVE FILE...: create ARCHIVE holding only the bytes tar itself adds.
# macOS bsdtar would otherwise store extended attributes such as com.apple.provenance,
# as a pax header (--no-mac-metadata) or as an AppleDouble "._" member (the default).
plain_c() {
  case $flavor in
    bsd) COPYFILE_DISABLE=1 tar --no-xattrs -cf "$@" ;;
    *) tar -cf "$@" ;;
  esac
}

# exists PATH: print yes if PATH exists or is a (possibly dangling) symlink, else no.
exists() {
  if [ -e "$1" ] || [ -L "$1" ]; then echo yes; else echo no; fi
}

# kind PATH: print symlink, dir, file, or none.
kind() {
  if [ -L "$1" ]; then
    echo symlink
  elif [ -d "$1" ]; then
    echo dir
  elif [ -f "$1" ]; then
    echo file
  else
    echo none
  fi
}

# mode_of FILE: print the ten-character type and permission string from ls -l.
mode_of() {
  # shellcheck disable=SC2012 # the ls -l mode string is the value under test
  ls -ld "$1" | cut -c1-10
}

# members ARCHIVE [TAR-OPTION...]: print the sorted member names of ARCHIVE on one line,
# or "error" when tar fails.
members() {
  m_archive=$1
  shift
  if m_list=$(tar "$@" -tf "$m_archive" 2>/dev/null); then
    printf '%s\n' "$m_list" | LC_ALL=C sort | awk '
      NR > 1 { printf " " }
      { printf "%s", $0 }
      END { print "" }'
  else
    echo error
  fi
}

# first_header ARCHIVE: decode the first 512-byte header of ARCHIVE with od and awk,
# recompute its checksum as POSIX defines it (unsigned byte sum, chksum field read as
# eight spaces), and print typeflag, the first 13 bytes of name, the prefix length,
# magic and version as escaped bytes, size, and whether the stored checksum matches.
first_header() {
  od -An -v -tu1 -N512 "$1" | awk '
    function field_octal(start, len,   i, v, seen) {
      v = 0; seen = 0
      for (i = start; i < start + len; i++) {
        if (b[i] >= 48 && b[i] <= 55) { v = v * 8 + b[i] - 48; seen = 1 }
        else if (seen) break
      }
      return v
    }
    function field_text(start, len,   i, s) {
      s = ""
      for (i = start; i < start + len && b[i] != 0; i++) s = s sprintf("%c", b[i])
      return s
    }
    function field_escaped(start, len,   i, s) {
      s = ""
      for (i = start; i < start + len; i++) {
        if (b[i] > 32 && b[i] < 127) s = s sprintf("%c", b[i])
        else s = s sprintf("\\%03o", b[i])
      }
      return s
    }
    { for (i = 1; i <= NF; i++) b[n++] = $i + 0 }
    END {
      sum = 0
      for (i = 0; i < 512; i++) sum += (i >= 148 && i < 156) ? 32 : b[i]
      type = b[156] == 0 ? "NUL" : sprintf("%c", b[156])
      printf "type=%s name=%s prefix_len=%d magic=%s size=%d chksum=%s\n",
        type, substr(field_text(0, 100), 1, 13), length(field_text(345, 155)),
        field_escaped(257, 8), field_octal(124, 12),
        (field_octal(148, 8) == sum ? "ok" : "bad")
    }'
}

# bytes FILE: print the size of FILE in bytes.
bytes() {
  wc -c <"$1" | awk '{ print $1 }'
}

# mktar ARGS...: run the archive builder.
mktar() {
  "${MKTAR:?MKTAR must name the compiled mktar}" "$@"
}

# extract_into DIR ARCHIVE: extract ARCHIVE inside DIR; print tar's exit status.
extract_into() {
  (cd "$1" && tar -xf "$2" 2>/dev/null)
  echo $?
}

case ${1-} in
  flavor)
    echo "$flavor"
    ;;
  header)
    # One 3-byte file: decode the header that tar wrote and the archive's total size.
    printf 'hi\n' >hello.txt
    plain_c h.tar hello.txt 2>/dev/null
    # printf, not echo: dash's echo would turn the escaped magic \040 back into a space.
    printf '%s\n' "$(first_header h.tar) bytes=$(bytes h.tar)"
    ;;
  longname)
    # A single 120-byte component cannot fit name[100] and has no slash to split at.
    long=$(printf '%0120d' 0 | tr 0 a)
    : >"$long"
    plain_c l.tar "$long" 2>/dev/null
    back=$(tar -tf l.tar 2>/dev/null)
    rt=no
    if [ "$back" = "$long" ]; then rt=yes; fi
    printf '%s\n' "$(first_header l.tar) roundtrip=$rt"
    ;;
  prefix)
    # 60 + 1 + 60 = 121 bytes: too long for name[100], but splits at the slash.
    d=$(printf '%060d' 0 | tr 0 d)
    f=$(printf '%060d' 0 | tr 0 f)
    mkdir "$d" && : >"$d/$f"
    plain_c p.tar "$d/$f" 2>/dev/null
    back=$(tar -tf p.tar 2>/dev/null)
    rt=no
    if [ "$back" = "$d/$f" ]; then rt=yes; fi
    printf '%s\n' "$(first_header p.tar) roundtrip=$rt"
    ;;
  dotdot)
    # A member named ../escape, extracted inside out/, would land in the scratch root.
    mktar f 0644 ../escape evil f 0644 ok fine >a.tar || exit 2
    rc=$(extract_into out ../a.tar)
    echo "rc=$rc escaped=$(exists escape) stripped=$(exists out/escape) ok=$(exists out/ok)"
    ;;
  absolute)
    # A member with an absolute name that points into the scratch directory.
    mktar f 0644 "$work/abs-target" evil >a.tar || exit 2
    rc=$(extract_into out ../a.tar)
    echo "rc=$rc written=$(exists "$work/abs-target") stripped=$(exists "out$work/abs-target")"
    ;;
  symlink-one)
    # One archive: a symlink esc -> ../outside, then a file esc/pwned beneath it.
    mktar l 0777 esc ../outside f 0644 esc/pwned evil >a.tar || exit 2
    rc=$(extract_into out ../a.tar)
    echo "rc=$rc escaped=$(exists outside/pwned) esc=$(kind out/esc)"
    ;;
  symlink-two)
    # Two archives into one directory: the first plants the symlink, the second writes
    # through it (the GNU tar case is CVE-2025-45582).
    mktar l 0777 esc ../outside >a.tar || exit 2
    mktar f 0644 esc/pwned evil >b.tar || exit 2
    rca=$(extract_into out ../a.tar)
    rcb=$(extract_into out ../b.tar)
    echo "rc_a=$rca rc_b=$rcb escaped=$(exists outside/pwned) esc=$(kind out/esc)"
    ;;
  glob)
    # A file named like an option, picked up by an unanchored glob.
    mkdir g && : >g/a && : >g/b && : >g/--exclude=a
    (cd g && plain_c ../star.tar -- * 2>/dev/null)
    # shellcheck disable=SC2035 # the bare glob turning a file name into an option is the behavior under test
    (cd g && plain_c ../star_nodd.tar * 2>/dev/null)
    (cd g && plain_c ../dot.tar ./* 2>/dev/null)
    echo "star=$(members star_nodd.tar) dashdash=$(members star.tar) dotslash=$(members dot.tar)"
    ;;
  concat)
    # cat of two archives: the first archive's end-of-archive blocks hide the second.
    : >one && : >two
    plain_c a.tar one 2>/dev/null
    plain_c b.tar two 2>/dev/null
    cat a.tar b.tar >c.tar
    echo "plain=$(members c.tar) ignore_zeros=$(members c.tar --ignore-zeros)"
    ;;
  perms)
    # Modes 4755 and 0777 in the archive, extracted by a non-root user with umask 022.
    mktar f 4755 suid.sh evil f 0777 open.sh fine >a.tar || exit 2
    rc=$(extract_into out ../a.tar)
    echo "rc=$rc suid=$(mode_of out/suid.sh) open=$(mode_of out/open.sh)"
    ;;
  repro)
    # Same contents, created in a different order and with different mtimes.
    mkdir r1 r1/d && printf 'B\n' >r1/d/b && printf 'A\n' >r1/a
    mkdir r2 && printf 'A\n' >r2/a && mkdir r2/d && printf 'B\n' >r2/d/b
    touch -t 202001010000 r1/a r1/d/b r1/d r1
    touch -t 202101010000 r2/a r2/d/b r2/d r2
    (cd r1 && plain_c ../n1.tar . 2>/dev/null)
    (cd r2 && plain_c ../n2.tar . 2>/dev/null)
    naive=same
    cmp -s n1.tar n2.tar || naive=differ
    # The recipe: pin every mtime, feed a byte-sorted list, never recurse, and pin owners.
    for r in r1 r2; do
      (
        cd "$r" || exit 1
        find . -exec touch -t 200001010000 {} +
        find . ! -name . -print | LC_ALL=C sort >../"$r".list
        case $flavor in
          gnu) tar --no-recursion --owner=0 --group=0 --numeric-owner \
            --format=ustar -cf ../"$r".tar -T ../"$r".list ;;
          bsd) COPYFILE_DISABLE=1 tar --no-xattrs -n --uid 0 --gid 0 --uname root \
            --gname root --format ustar -cf ../"$r".tar -T ../"$r".list ;;
          busybox) tar --no-recursion -cf ../"$r".tar -T ../"$r".list ;;
          *) exit 1 ;;
        esac
      ) 2>/dev/null
    done
    pinned=same
    cmp -s r1.tar r2.tar || pinned=differ
    echo "naive=$naive pinned=$pinned members=$(members r1.tar)"
    ;;
  *)
    echo "usage: tarlab.sh flavor|header|longname|prefix|dotdot|absolute|symlink-one|symlink-two|glob|concat|perms|repro" >&2
    exit 2
    ;;
esac
