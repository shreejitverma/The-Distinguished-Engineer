#!/usr/bin/env sh
# Demonstrating atomic noclobber protection against pre-existing files/symlinks
set -eu

target_path="$1"
payload="$2"

# Enable noclobber
set -C

# Attempt to write to target path
# Under POSIX XCU 2.7.2, redirection with > must fail if file or symlink to regular file exists
# Redirect stderr before stdout so any redirection diagnostic from the shell is silenced
if 2>/dev/null printf '%s\n' "$payload" >"$target_path"; then
  printf 'RESULT: SUCCESS\n'
else
  printf 'RESULT: BLOCKED\n'
fi
