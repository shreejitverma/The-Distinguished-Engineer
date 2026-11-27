#!/usr/bin/env sh
# Secure temporary directory pattern
set -eu

parent_dir="${1:-.}"

# Ensure restrictive umask so created directories are private (0700)
old_umask=$(umask)
umask 077

# Create private temporary directory atomically
workdir=$(mktemp -d "${parent_dir}/secure_job.XXXXXX") || exit 1

# Install cleanup trap across normal exit and terminating signals
trap 'rm -rf -- "$workdir"' EXIT HUP INT TERM

# Restore umask if needed
umask "$old_umask"

# Inside the private 0700 directory, fixed filenames are immune to external symlink races
job_file="${workdir}/payload.dat"
printf '%s\n' "PROTECTED_DATA" >"$job_file"

printf 'DIR_CREATED: %s\n' "$workdir"
payload=$(cat "$job_file")
printf 'PAYLOAD: %s\n' "$payload"
