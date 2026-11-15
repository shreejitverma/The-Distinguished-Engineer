#!/bin/sh
# verify-manifest.sh MANIFEST: verify a sha256sum-format manifest strictly, in any POSIX shell.
#
# Every line must be 64 lowercase hex digits, a space, a mode flag (a space or '*'), and a
# relative file name, resolved from the current directory as `sha256sum -c` resolves it.
# Unlike `sha256sum -c`, it never skips a line it cannot parse: a malformed line, an unsafe
# name (absolute, or with a `..` component), a missing file, or a mismatch makes it exit 1,
# and so does a manifest with no lines at all. It exits 2 on a usage or tool error.
# Each file is hashed through standard input, so the tool never escapes its name.
# SHA256SUM names the hashing command (default sha256sum); it must print the hex digest first.

set -u
LC_ALL=C
export LC_ALL

die() {
  printf 'verify-manifest: %s\n' "$*" >&2
  exit 2
}

[ "$#" -eq 1 ] || die "usage: verify-manifest.sh MANIFEST"
manifest=$1
[ -f "$manifest" ] || die "$manifest: not a regular file"
tool=${SHA256SUM:-sha256sum}
command -v "$tool" >/dev/null 2>&1 || die "$tool: not found (set SHA256SUM)"

complain() {
  printf 'verify-manifest: %s:%s: %s\n' "$manifest" "$lineno" "$1" >&2
  bad=1
}

lineno=0
bad=0
while IFS= read -r line || [ -n "$line" ]; do
  lineno=$((lineno + 1))
  want=${line%% *}
  rest=${line#* }
  case $want in
    *[!0-9a-f]*) want= ;;
    *) ;;
  esac
  if [ "${#want}" -ne 64 ] || [ "$rest" = "$line" ]; then
    complain "malformed checksum line"
    continue
  fi
  case $rest in
    " "?* | "*"?*) name=${rest#?} ;;
    *)
      complain "malformed checksum line"
      continue
      ;;
  esac
  case /$name/ in
    //* | */../*)
      complain "unsafe file name"
      continue
      ;;
    *) ;;
  esac
  if [ ! -f "$name" ] || [ ! -r "$name" ]; then
    printf '%s: FAILED open or read\n' "$name"
    bad=1
    continue
  fi
  if ! got=$("$tool" <"$name" 2>/dev/null); then
    printf '%s: FAILED open or read\n' "$name"
    bad=1
    continue
  fi
  got=${got%% *}
  [ "${#got}" -eq 64 ] || die "$tool printed an unexpected digest for $name"
  if [ "$got" = "$want" ]; then
    printf '%s: OK\n' "$name"
  else
    printf '%s: FAILED\n' "$name"
    bad=1
  fi
done <"$manifest"

if [ "$lineno" -eq 0 ]; then
  printf 'verify-manifest: %s: no checksum lines\n' "$manifest" >&2
  exit 1
fi
[ "$bad" -eq 0 ] || exit 1
exit 0
