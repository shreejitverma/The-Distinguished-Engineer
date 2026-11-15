#!/bin/sh
set -eu

mode="${1:-}"
dir="${2:-.}"
probe="${3:-}"

case "$mode" in
  semi_proc_count)
    # Each matching file triggers one execution of the target utility
    find "$dir" -type f -name 'test_*.txt' -exec "${probe:-true}" {} \;
    ;;
  plus_proc_count)
    # Matching files are aggregated into one batch execution
    find "$dir" -type f -name 'test_*.txt' -exec "${probe:-true}" {} +
    ;;
  exit_status_semi)
    # In semicolon mode, a non-zero utility exit status does NOT cause find to fail
    set +e
    find "$dir" -type f -name 'test_*.txt' -exec false {} \;
    st=$?
    set -e
    printf '%d\n' "$st"
    ;;
  exit_status_plus)
    # In plus mode, any non-zero utility exit status forces find to exit non-zero
    set +e
    find "$dir" -type f -name 'test_*.txt' -exec false {} +
    st=$?
    set -e
    printf '%d\n' "$st"
    ;;
  special_names_plus)
    # Batch execution with -exec ... {} + safely preserves spaces, newlines, quotes, and wildcards
    find "$dir" -type f -name 'spec_*' -exec "${probe:?missing probe}" {} +
    ;;
  special_names_print0)
    # Piping -print0 into xargs -0 safely preserves spaces, newlines, quotes, and wildcards
    find "$dir" -type f -name 'spec_*' -print0 | xargs -0 "${probe:?missing probe}"
    ;;
  zero_matches_plus)
    # When zero files match, -exec ... {} + never invokes the command
    find "$dir" -type f -name 'nonexistent_*' -exec "${probe:?missing probe}" {} +
    ;;
  syntax_trailing_plus)
    # POSIX requires + to immediately follow {}
    # Intervening arguments make + ordinary and trigger syntax errors
    set +e
    find "$dir" -type f -name 'test_*.txt' -exec echo {} extra + 2>/dev/null
    st=$?
    set -e
    printf '%d\n' "$st"
    ;;
  pipe_masking)
    # A standard pipeline masks find failure unless pipefail is enabled
    set +e
    find "$dir/nonexistent_subdir" -type f -print0 2>/dev/null | xargs -0 "${probe:-true}" >/dev/null 2>&1
    st=$?
    set -e
    printf '%d\n' "$st"
    ;;
  read_null_stream)
    # Reads null-delimited stream into shell variables
    # bash and zsh support read -d ''; dash and posh do not
    if (printf 'x\0' | eval 'read -r -d "" _' 2>/dev/null); then
      find "$dir" -type f -name 'spec_*' -print0 | {
        n=0
        # shellcheck disable=SC3045 # read -d is an Issue 8 / extension feature tested conditionally
        while IFS= read -r -d '' _; do
          n=$((n + 1))
        done
        printf '%d entries\n' "$n"
      }
    else
      printf 'unsupported\n'
    fi
    ;;
  *)
    printf 'Unknown mode: %s\n' "$mode" >&2
    exit 1
    ;;
esac
