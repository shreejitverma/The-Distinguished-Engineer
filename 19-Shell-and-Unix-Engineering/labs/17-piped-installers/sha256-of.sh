#!/bin/sh
# sha256-of.sh: print the SHA-256 of a file as 64 lowercase hex digits, with whichever tool exists.
# Usage: sha256-of.sh FILE
# GNU coreutils and BusyBox ship sha256sum; macOS always ships shasum (a Perl script), and the 27.3 host here also has /sbin/sha256sum.
# Both print "DIGEST  NAME" with two spaces; reading from stdin makes NAME a "-".
set -u
if command -v sha256sum >/dev/null 2>&1; then
  sum=$(sha256sum <"$1") || exit 69
elif command -v shasum >/dev/null 2>&1; then
  sum=$(shasum -a 256 <"$1") || exit 69
else
  echo "sha256-of.sh: neither sha256sum nor shasum is installed" >&2
  exit 69
fi
printf '%s\n' "${sum%% *}"
