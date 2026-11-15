#!/bin/sh
# compress_probe.sh MODE [ARG...]: read gzip, zstd, and xz streams byte by byte.
#
# Modes:
#   flavor              print the gzip implementation: gnu, apple, bsd, busybox, or unknown
#   magic FILE          name the format from the leading bytes, never the suffix:
#                       gzip, zstd, zstd-skippable, xz, bzip2, lz4, or unknown
#   gzip-header FILE    print the RFC 1952 fixed header of the first member (and FNAME)
#   gzip-trailer FILE   print CRC32 (hex) and ISIZE (decimal) from the last 8 bytes
#   zstd-header FILE    print the RFC 8878 frame header descriptor of the first frame
#   xz-flags FILE       print the check type, the footer magic, and the size modulo 4
#   skippable TEXT      write a zstd skippable frame (magic 0x184D2A50) holding TEXT
#
# Every number is parsed with od and awk, so the script needs no interpreter beyond sh.
# awk does the arithmetic because mksh integers are 32-bit and MTIME and ISIZE are unsigned 32-bit.
set -u

die() {
  printf 'compress_probe: %s\n' "$*" >&2
  exit 2
}

# dec_bytes FILE COUNT: the first COUNT bytes of FILE as decimal numbers on one line.
dec_bytes() {
  head -c "$2" "$1" | od -An -tu1 -v | tr -s ' \n' '  '
}

# tail_bytes FILE COUNT: the last COUNT bytes of FILE as decimal numbers on one line.
tail_bytes() {
  tail -c "$2" "$1" | od -An -tu1 -v | tr -s ' \n' '  '
}

gzip_flavor() {
  version=$(gzip --version 2>&1 </dev/null) || true
  case $version in
    *"Apple gzip"*) echo apple ;;
    *"FreeBSD gzip"* | *"NetBSD gzip"*) echo bsd ;;
    *BusyBox*) echo busybox ;;
    "gzip "[0-9]*) echo gnu ;;
    *) echo unknown ;;
  esac
}

magic() {
  [ -r "$1" ] || die "cannot read $1"
  dec_bytes "$1" 6 | awk '{
    if ($1 == 31 && $2 == 139) print "gzip"
    else if ($1 == 40 && $2 == 181 && $3 == 47 && $4 == 253) print "zstd"
    else if ($1 >= 80 && $1 <= 95 && $2 == 42 && $3 == 77 && $4 == 24) print "zstd-skippable"
    else if ($1 == 253 && $2 == 55 && $3 == 122 && $4 == 88 && $5 == 90 && $6 == 0) print "xz"
    else if ($1 == 66 && $2 == 90 && $3 == 104) print "bzip2"
    else if ($1 == 4 && $2 == 34 && $3 == 77 && $4 == 24) print "lz4"
    else print "unknown"
  }'
}

gzip_header() {
  [ -r "$1" ] || die "cannot read $1"
  dec_bytes "$1" 266 | awk '{
    if ($1 != 31 || $2 != 139) { print "not gzip"; exit 1 }
    flg = $4
    mtime = $5 + 256 * $6 + 65536 * $7 + 16777216 * $8
    printf "id=1f8b cm=%d flg=%02x mtime=%.0f xfl=%d os=%d", $3, flg, mtime, $9, $10
    # FNAME (bit 3) follows the 10-byte header unless FEXTRA (bit 2) puts XLEN bytes first.
    if (int(flg / 8) % 2 == 1 && int(flg / 4) % 2 == 0) {
      name = ""
      for (i = 11; i <= NF && $i != 0; i++) name = name sprintf("%c", $i)
      printf " name=%s", name
    }
    printf "\n"
  }'
}

gzip_trailer() {
  [ -r "$1" ] || die "cannot read $1"
  tail_bytes "$1" 8 | awk '{
    isize = $5 + 256 * $6 + 65536 * $7 + 16777216 * $8
    printf "crc32=%02x%02x%02x%02x isize=%.0f\n", $4, $3, $2, $1, isize
  }'
}

zstd_header() {
  [ -r "$1" ] || die "cannot read $1"
  dec_bytes "$1" 6 | awk '{
    if ($1 != 40 || $2 != 181 || $3 != 47 || $4 != 253) { print "not zstd"; exit 1 }
    fhd = $5
    fcs_flag = int(fhd / 64)
    single = int(fhd / 32) % 2
    checksum = int(fhd / 4) % 2
    if (fcs_flag == 0) fcs_bytes = single ? 1 : 0
    else if (fcs_flag == 1) fcs_bytes = 2
    else if (fcs_flag == 2) fcs_bytes = 4
    else fcs_bytes = 8
    printf "single_segment=%d checksum=%d content_size=%s", single, checksum, fcs_bytes ? "present" : "absent"
    if (!single) {
      # Window_Descriptor: windowLog = 10 + Exponent; add Mantissa eighths (RFC 8878 3.1.1.1.2).
      base = 2 ^ (10 + int($6 / 8))
      printf " window=%.0f", base + (base / 8) * ($6 % 8)
    }
    printf "\n"
  }'
}

xz_flags() {
  [ -r "$1" ] || die "cannot read $1"
  size=$(wc -c <"$1")
  footer=$(tail_bytes "$1" 2)
  dec_bytes "$1" 8 | awk -v size="$size" -v footer="$footer" '{
    if ($1 != 253 || $2 != 55 || $3 != 122) { print "not xz"; exit 1 }
    id = $8 % 16
    if (id == 0) check = "none"
    else if (id == 1) check = "crc32"
    else if (id == 4) check = "crc64"
    else if (id == 10) check = "sha256"
    else check = "reserved"
    split(footer, f, " ")
    # The footer magic is "YZ" (0x59 0x5A) unless Stream Padding (NUL bytes) follows it.
    printf "check=%s footer=%s size_mod4=%d\n", check, (f[1] == 89 && f[2] == 90) ? "YZ" : "other", size % 4
  }'
}

# skippable TEXT: magic 0x184D2A50 little-endian, then Frame_Size as 4 little-endian bytes.
skippable() {
  len=${#1}
  [ "$len" -lt 256 ] || die "skippable: keep TEXT under 256 bytes"
  # shellcheck disable=SC2059 # the format is built on purpose to emit one octal byte
  printf '\120\052\115\030'"\\$(printf '%03o' "$len")"'\000\000\000'
  printf '%s' "$1"
}

[ "$#" -ge 1 ] || die "usage: compress_probe.sh MODE [ARG...]"
mode=$1
shift
case $mode in
  flavor) gzip_flavor ;;
  magic) magic "$1" ;;
  gzip-header) gzip_header "$1" ;;
  gzip-trailer) gzip_trailer "$1" ;;
  zstd-header) zstd_header "$1" ;;
  xz-flags) xz_flags "$1" ;;
  skippable) skippable "$1" ;;
  *) die "unknown mode: $mode" ;;
esac
