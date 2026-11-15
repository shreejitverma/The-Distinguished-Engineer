#!/bin/sh
# traps.sh: probe userland utility portability traps and POSIX workarounds.
# Usage: traps.sh MODE PARENT_DIR
# Each run creates an isolated scratch directory under PARENT_DIR.
# Output is key=value lines computed from actual command execution.

set -eu

mode=${1:?usage: traps.sh MODE PARENT_DIR}
parent=${2:?usage: traps.sh MODE PARENT_DIR}
work=$(mktemp -d "$parent/traps.XXXXXX") || exit 99
cd "$work" || exit 99
mkdir t && cd t || exit 99

case $mode in
  permute)
    # Tests whether option parsing permutes options appearing after operands.
    # GNU getopt permutes by default; BSD getopt stops at the first non-option operand.
    mkdir target
    if ls target -d >/dev/null 2>&1; then
      echo "permuted=yes"
    else
      echo "permuted=no"
    fi
    if POSIXLY_CORRECT=1 ls target -d >/dev/null 2>&1; then
      echo "permuted_posix=yes"
    else
      echo "permuted_posix=no"
    fi
    ;;

  head_neg)
    # Tests negative line counts: GNU head supports -n -1; BSD head rejects it.
    # POSIX standard alternative is sed '$d'.
    printf 'alpha\nbravo\ncharlie\n' >f
    if head -n -1 f >out 2>err; then
      echo "head_neg_status=0"
      printf 'head_neg_lines='
      tr '\n' '|' <out
      printf '\n'
    else
      echo "head_neg_status=fail"
    fi
    sed '$d' f >sed_out
    printf 'sed_fallback='
    tr '\n' '|' <sed_out
    printf '\n'
    ;;

  wc_padding)
    # Tests leading whitespace in wc -l output from stdin.
    # BSD wc formats with leading spaces; GNU wc omits spaces when reading stdin.
    printf 'alpha\nbravo\ncharlie\n' | wc -l >wc_raw
    raw=$(cat wc_raw)
    # Count leading spaces
    trimmed=$(printf '%s' "$raw" | tr -d ' ')
    spaces=$(($(printf '%s' "$raw" | wc -c) - $(printf '%s' "$trimmed" | wc -c)))
    echo "wc_raw_trimmed=$trimmed"
    if [ "$spaces" -gt 0 ]; then
      echo "wc_has_padding=yes"
    else
      echo "wc_has_padding=no"
    fi
    # Portable shell arithmetic strips whitespace automatically
    normalized=$(($(cat wc_raw)))
    echo "wc_normalized=$normalized"
    ;;

  which_builtin)
    # Tests resolution of a shell builtin (echo) with which vs command -v.
    # which inspects PATH on disk; command -v recognizes shell builtins.
    # "command which" skips a shell's own "which" builtin (zsh has one) and runs the external program.
    which_out=$(command which echo 2>/dev/null || echo "<none>")
    command_out=$(command -v echo 2>/dev/null || echo "<none>")
    case $which_out in
      /*) echo "which_finds_disk_binary=yes" ;;
      *) echo "which_finds_disk_binary=no" ;;
    esac
    case $command_out in
      echo | *builtin*) echo "command_v_finds_builtin=yes" ;;
      *) echo "command_v_finds_builtin=no" ;;
    esac
    ;;

  stat_format)
    # Tests stat format specifiers across GNU (-c %s), BSD (-f %z), and POSIX wc -c.
    printf '0123456789' >f
    # GNU stat reads -f as "file system status" and prints that to stdout before failing,
    # so test the exit status and discard the output instead of using "|| echo".
    if gnu_size=$(stat -c '%s' f 2>/dev/null); then :; else gnu_size=fail; fi
    if bsd_size=$(stat -f '%z' f 2>/dev/null); then :; else bsd_size=fail; fi
    posix_size=$(wc -c <f | tr -d ' ')
    echo "gnu_stat_size=$gnu_size"
    echo "bsd_stat_size=$bsd_size"
    echo "posix_wc_size=$posix_size"
    ;;

  tr_brackets)
    # Tests character translation bracket trap.
    # In POSIX and BSD tr, [a-z] literally contains [ and ], mapping [ to A.
    # shellcheck disable=SC2021,SC2019 # intentionally testing literal bracket trap
    t_mismatch=$(printf '[hello]' | tr '[a-z]' 'A-Z')
    # shellcheck disable=SC2018,SC2019 # intentionally comparing character range vs class
    t_range=$(printf '[hello]' | tr 'a-z' 'A-Z')
    t_posix=$(printf '[hello]' | tr '[:lower:]' '[:upper:]')
    echo "tr_mismatch=$t_mismatch"
    echo "tr_range=$t_range"
    echo "tr_posix=$t_posix"
    ;;

  cut_delim)
    # Tests a multi-character cut delimiter: POSIX requires one character.
    # GNU, BSD, and uutils reject it; BusyBox silently keeps the first character.
    printf 'a::b\n' >f
    if cut -d '::' -f 1 f >out 2>/dev/null; then
      cut_out=$(cat out)
      echo "cut_multichar_status=0"
      echo "cut_multichar_out=$cut_out"
    else
      echo "cut_multichar_status=fail"
    fi
    awk_out=$(awk -F '::' '{ print $1 }' f)
    echo "awk_split=$awk_out"
    ;;

  tail_r)
    # Tests tail -r (BSD line reversal, not POSIX) against the portable awk reversal.
    printf 'alpha\nbravo\ncharlie\n' >f
    if tail -r f >out 2>/dev/null; then
      echo "tail_r_status=0"
      printf 'tail_r_lines='
      tr '\n' '|' <out
      printf '\n'
    else
      echo "tail_r_status=fail"
    fi
    awk '{ a[NR] = $0 } END { for (i = NR; i >= 1; i--) print a[i] }' f >awk_out
    printf 'awk_reverse='
    tr '\n' '|' <awk_out
    printf '\n'
    ;;

  df_portable)
    # Tests POSIX df -P output structure: exactly one header and 6 columns.
    df -P . >df_raw
    header_ok="no"
    if head -n 1 df_raw | grep 'Filesystem' >/dev/null 2>&1; then
      header_ok="yes"
    fi
    col_count=$(sed -n '2p' df_raw | awk '{ print NF }')
    echo "df_header_ok=$header_ok"
    echo "df_col_count=$col_count"
    ;;

  arg_injection)
    # Tests command-line option injection and double-dash delimiter.
    # A file named -n would trigger option parsing in cat unless -- is used.
    printf 'secret_payload\n' >'-n'
    safe_out=$(cat -- -n)
    echo "safe_cat=$safe_out"
    ;;

  *)
    echo "unknown mode: $mode" >&2
    exit 1
    ;;
esac
