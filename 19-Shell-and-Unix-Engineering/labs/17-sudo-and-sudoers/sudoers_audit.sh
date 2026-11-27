#!/bin/sh
# sudoers_audit.sh: audits sudoers configuration files for critical anti-patterns,
# privilege escalation risks, wildcard argument traps, and syntax errors.
#
# Usage:
#   sudoers_audit.sh <sudoers-file>

set -eu

if [ "$#" -lt 1 ]; then
  printf 'Usage: %s <sudoers-file>\n' "$0" >&2
  exit 2
fi

target="$1"

if [ ! -f "$target" ]; then
  printf 'Error: file not found: %s\n' "$target" >&2
  exit 2
fi

critical_count=0
warning_count=0

report() {
  severity="$1"
  line_no="$2"
  msg="$3"
  printf '[%s] line %s: %s\n' "$severity" "$line_no" "$msg"
  if [ "$severity" = "CRITICAL" ]; then
    critical_count=$((critical_count + 1))
  elif [ "$severity" = "WARNING" ]; then
    warning_count=$((warning_count + 1))
  fi
}

# 1. Run visudo syntax validation if available
if command -v visudo >/dev/null 2>&1; then
  if ! visudo_err=$(visudo -c -s -f "$target" 2>&1); then
    report "CRITICAL" "0" "visudo syntax validation failed: $visudo_err"
  fi
fi

# 2. Analyze directives line by line
line_num=0
has_env_reset=0
has_secure_path=0

while IFS= read -r raw_line || [ -n "$raw_line" ]; do
  line_num=$((line_num + 1))

  # Strip comments (unless #include or #includedir)
  case "$raw_line" in
    \#include*)
      clean_line="$raw_line"
      ;;
    \#*)
      continue
      ;;
    *)
      clean_line="${raw_line%%#*}"
      ;;
  esac

  # Trim leading and trailing whitespace
  clean_line=$(printf '%s' "$clean_line" | awk '{$1=$1};1')
  [ -n "$clean_line" ] || continue

  # Check Defaults settings
  case "$clean_line" in
    *env_reset*)
      has_env_reset=1
      ;;
    *) ;;
  esac
  case "$clean_line" in
    *secure_path=*)
      has_secure_path=1
      ;;
    *) ;;
  esac

  # Check for reserved words used as alias names
  case "$clean_line" in
    *Alias[[:space:]]ALL[[:space:]]* | *Alias[[:space:]]ALL=*)
      report "CRITICAL" "$line_num" "reserved word ALL used as alias name"
      ;;
    *Alias[[:space:]]CHROOT[[:space:]]* | *Alias[[:space:]]CHROOT=*)
      report "CRITICAL" "$line_num" "reserved word CHROOT used as alias name"
      ;;
    *Alias[[:space:]]CWD[[:space:]]* | *Alias[[:space:]]CWD=*)
      report "CRITICAL" "$line_num" "reserved word CWD used as alias name"
      ;;
    *Alias[[:space:]]TIMEOUT[[:space:]]* | *Alias[[:space:]]TIMEOUT=*)
      report "CRITICAL" "$line_num" "reserved word TIMEOUT used as alias name"
      ;;
    *) ;;
  esac

  # Check for ineffective command negation: ALL, !...
  case "$clean_line" in
    *=*\ ALL,\ !* | *=*ALL,\ !*)
      report "CRITICAL" "$line_num" "ineffective command subtraction: negation with ALL can be bypassed via copies, hardlinks, or secondary shells"
      ;;
    *) ;;
  esac

  # Check for wildcard argument crossing in command specifications
  case "$clean_line" in
    *"/bin/cat "*\* | *"/cat "*\*)
      report "CRITICAL" "$line_num" "wildcard argument crossing trap: cat argument ends in wildcard, allows reading arbitrary files"
      ;;
    *) ;;
  esac

  # Check for interactive editors or pagers granted without sudoedit or NOEXEC
  case "$clean_line" in
    *\ /usr/bin/vi\ * | *\ /bin/vi\ * | *\ /usr/bin/vi | *\ /bin/vi | *\ /usr/bin/vim* | *\ /usr/bin/nano*)
      case "$clean_line" in
        *NOEXEC:* | *sudoedit*)
          ;;
        *)
          report "CRITICAL" "$line_num" "editor granted without sudoedit or NOEXEC: allows shell escape to full root"
          ;;
      esac
      ;;
    *\ /usr/bin/less* | *\ /usr/bin/more* | *\ /usr/bin/view*)
      case "$clean_line" in
        *NOEXEC:*)
          ;;
        *)
          report "WARNING" "$line_num" "pager granted without NOEXEC: allows shell escape (:!sh)"
          ;;
      esac
      ;;
    *\ /usr/bin/awk* | *\ /usr/bin/find* | *\ /usr/bin/tee*)
      case "$clean_line" in
        *NOEXEC:*)
          ;;
        *)
          report "WARNING" "$line_num" "utility with shell execution or arbitrary file write granted: evaluate NOEXEC or restrict arguments"
          ;;
      esac
      ;;
    *) ;;
  esac

  # Check included directories for invalid file patterns
  case "$clean_line" in
    @includedir\ * | \#includedir\ *)
      inc_dir="${clean_line#*includedir }"
      inc_dir=$(printf '%s' "$inc_dir" | awk '{$1=$1};1')
      if [ -d "$inc_dir" ]; then
        for f in "$inc_dir"/*; do
          [ -e "$f" ] || continue
          base="${f##*/}"
          case "$base" in
            *~ | *.*)
              report "WARNING" "$line_num" "file $base in includedir contains . or ends in ~ and will be silently ignored by sudo"
              ;;
            *) ;;
          esac
        done
      fi
      ;;
    *) ;;
  esac
done <"$target"

if [ "$has_env_reset" -eq 0 ]; then
  report "WARNING" "0" "missing explicit Defaults env_reset (recommended for defensive hygiene)"
fi

if [ "$has_secure_path" -eq 0 ]; then
  report "WARNING" "0" "missing explicit Defaults secure_path (recommended to prevent PATH hijacking)"
fi

printf 'Audit complete: %d critical, %d warnings\n' "$critical_count" "$warning_count"

if [ "$critical_count" -gt 0 ]; then
  exit 1
fi
exit 0
