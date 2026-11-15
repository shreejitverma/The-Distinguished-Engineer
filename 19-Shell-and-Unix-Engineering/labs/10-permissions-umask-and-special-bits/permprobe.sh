#!/usr/bin/env sh
# permprobe.sh - probe permission bits, umask calculation, and special bits.

set -eu

PROBE_DIR="${TMPDIR:-/tmp}/permprobe.$$"
mkdir -p "$PROBE_DIR"

cleanup() {
  rm -rf "$PROBE_DIR"
}
trap cleanup EXIT INT TERM

# Ensure getmode helper is compiled and available
GETMODE_BIN="${GETMODE_BIN:-$PROBE_DIR/getmode}"
if [ ! -x "$GETMODE_BIN" ]; then
  SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
  if [ -f "$SRC_DIR/getmode.c" ]; then
    "${CC:-cc}" -O2 -o "$GETMODE_BIN" "$SRC_DIR/getmode.c"
  else
    printf "getmode.c not found\n" >&2
    exit 1
  fi
fi

file_mode() {
  "$GETMODE_BIN" "$1"
}

cmd="${1:-}"

case "$cmd" in
  umask-calc)
    sub="$PROBE_DIR/ucalc"
    mkdir -p "$sub"

    (
      umask 0022
      : >"$sub/f_022"
      mkdir "$sub/d_022"
    )
    (
      umask 0077
      : >"$sub/f_077"
      mkdir "$sub/d_077"
    )
    (
      umask 0027
      : >"$sub/f_027"
      mkdir "$sub/d_027"
    )

    m_f022=$(file_mode "$sub/f_022")
    m_d022=$(file_mode "$sub/d_022")
    m_f077=$(file_mode "$sub/f_077")
    m_d077=$(file_mode "$sub/d_077")
    m_f027=$(file_mode "$sub/f_027")
    m_d027=$(file_mode "$sub/d_027")

    printf "u022_file=%s u022_dir=%s u077_file=%s u077_dir=%s u027_file=%s u027_dir=%s\n" \
      "$m_f022" "$m_d022" "$m_f077" "$m_d077" "$m_f027" "$m_d027"
    ;;

  umask-format)
    # Print bare umask, symbolic umask, and test umask -p
    octal=$(umask)
    symbolic=$(umask -S)
    p_status=0
    # shellcheck disable=SC3045 # intentional extension probe across shells
    umask -p >/dev/null 2>&1 || p_status=$?

    printf "octal=%s symbolic=%s p_status=%d\n" "$octal" "$symbolic" "$p_status"
    ;;

  negative-perms)
    sub="$PROBE_DIR/neg"
    mkdir -p "$sub"
    file="$sub/neg.txt"

    printf "payload\n" >"$file"
    chmod 0077 "$file"

    rc_read=0
    cat "$file" >/dev/null 2>&1 || rc_read=$?

    rc_write=0
    # Use sh -c to prevent premature redirection failure in the probe script
    sh -c ': >> "$1"' _ "$file" >/dev/null 2>&1 || rc_write=$?

    # Normalise return codes to 1 on failure
    [ "$rc_read" -eq 0 ] || rc_read=1
    [ "$rc_write" -eq 0 ] || rc_write=1

    printf "owner_read_rc=%d owner_write_rc=%d\n" "$rc_read" "$rc_write"
    ;;

  dir-perms)
    sub="$PROBE_DIR/dp"
    mkdir -p "$sub"
    dir="$sub/target"
    mkdir -p "$dir"
    printf "secret\n" >"$dir/secret.txt"

    # Case A: r-- without x: can read directory names, cannot traverse to stat/read files
    chmod 0400 "$dir"
    rc_ro=0
    cat "$dir/secret.txt" >/dev/null 2>&1 || rc_ro=$?

    # Restore write/search to alter mode and test Case B
    chmod 0700 "$dir"
    # Case B: --x without r: cannot list names, but can open known path
    chmod 0100 "$dir"
    rc_xo=0
    cat "$dir/secret.txt" >/dev/null 2>&1 || rc_xo=$?

    # Restore permissions to test file deletion
    chmod 0700 "$dir"
    printf "unwritable\n" >"$dir/zero.txt"
    chmod 0000 "$dir/zero.txt"
    rc_rm=0
    rm -f "$dir/zero.txt" 2>/dev/null || rc_rm=$?

    [ "$rc_ro" -eq 0 ] || rc_ro=1
    [ "$rc_xo" -eq 0 ] || rc_xo=1
    [ "$rc_rm" -eq 0 ] || rc_rm=1

    printf "read_only_traversal_rc=%d exec_only_read_rc=%d rm_mode0_rc=%d\n" \
      "$rc_ro" "$rc_xo" "$rc_rm"
    ;;

  sticky-dir)
    sub="$PROBE_DIR/st"
    mkdir -p "$sub"
    dir="$sub/sticky"
    mkdir -p "$dir"
    chmod 1777 "$dir"

    m_dir=$(file_mode "$dir")

    # Directory owner creates a file inside and verifies deletion succeeds
    printf "content\n" >"$dir/owned.txt"
    rc_del=0
    rm -f "$dir/owned.txt" 2>/dev/null || rc_del=$?
    [ "$rc_del" -eq 0 ] || rc_del=1

    printf "sticky_mode=%s dir_owner_unlink_rc=%d\n" "$m_dir" "$rc_del"
    ;;

  suid-drop)
    sub="$PROBE_DIR/suid"
    mkdir -p "$sub"
    file="$sub/suid_bin"

    printf "echo suid\n" >"$file"
    chmod 4755 "$file"
    m_before=$(file_mode "$file")

    # Append to file as unprivileged user
    printf "append\n" >>"$file"
    m_after=$(file_mode "$file")

    printf "before=%s after=%s\n" "$m_before" "$m_after"
    ;;

  chmod-umask)
    sub="$PROBE_DIR/cmask"
    mkdir -p "$sub"
    f1="$sub/f1"
    f2="$sub/f2"

    : >"$f1"
    : >"$f2"
    chmod 0644 "$f1" "$f2"

    (
      umask 0077
      chmod +x "$f1"
      chmod a+x "$f2"
    )

    m1=$(file_mode "$f1")
    m2=$(file_mode "$f2")

    printf "no_who=%s explicit_who=%s\n" "$m1" "$m2"
    ;;

  chmod-capital-x)
    sub="$PROBE_DIR/cx"
    mkdir -p "$sub"
    d="$sub/dir"
    data="$sub/data.txt"
    bin="$sub/bin"

    mkdir -p "$d"
    : >"$data"
    : >"$bin"

    chmod 0700 "$d"
    chmod 0600 "$data"
    chmod 0700 "$bin"

    chmod a+rX "$d" "$data" "$bin"

    m_d=$(file_mode "$d")
    m_data=$(file_mode "$data")
    m_bin=$(file_mode "$bin")

    printf "dir=%s data=%s bin=%s\n" "$m_d" "$m_data" "$m_bin"
    ;;

  *)
    printf "unknown command: %s\n" "$cmd" >&2
    exit 1
    ;;
esac
