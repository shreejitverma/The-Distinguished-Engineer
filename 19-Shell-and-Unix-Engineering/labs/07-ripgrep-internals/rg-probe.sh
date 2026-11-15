#!/bin/sh
# shellcheck disable=SC2312 # the $(joined)/$(bytes) helpers only format output; their status is irrelevant
# rg-probe: observe the ripgrep behaviors that a shell script inherits and
# POSIX grep does not have: leftmost-first matching, Unicode without a locale,
# automatic filtering, the NUL-byte binary heuristic, the stdin heuristic,
# exit statuses, broken-pipe handling, and the config file.
# Usage: rg-probe.sh semantics|filter|binary|stdin|exit|pipe|config
# Every file lives in a fresh mktemp -d directory under TMPDIR, removed on exit.
set -u

mode=${1:?usage: rg-probe.sh semantics|filter|binary|stdin|exit|pipe|config}
if ! command -v rg >/dev/null 2>&1; then
  printf 'rg-probe: rg (ripgrep) is not installed\n' >&2
  exit 127
fi
work=$(mktemp -d "${TMPDIR:-/tmp}/rglab.XXXXXX") || exit 1
trap 'rm -rf "$work"' EXIT

# rgx: ripgrep with any RIPGREP_CONFIG_PATH file ignored.
rgx() {
  rg --no-config "$@"
}

# bytes FILE: print the size of FILE in bytes, without wc's padding.
bytes() {
  wc -c <"$1" | tr -d ' '
}

# joined FILE: print the lines of FILE separated by single spaces.
joined() {
  tr '\n' ' ' <"$1" | sed 's/ $//'
}

semantics() {
  printf 'ab\n' >"$work/ab.txt"
  rgx -o 'a|ab' "$work/ab.txt" >"$work/o1"
  rgx -o 'ab|a' "$work/ab.txt" >"$work/o2"
  grep -oE 'a|ab' "$work/ab.txt" >"$work/o3"
  printf 'alternation rg=%s rg_swapped=%s grep=%s\n' \
    "$(joined "$work/o1")" "$(joined "$work/o2")" "$(joined "$work/o3")"

  # c a f, then U+00E9 as the two UTF-8 bytes 0xC3 0xA9.
  printf 'caf\303\251\n' >"$work/u8.txt"
  rgx -o '\w+' "$work/u8.txt" >"$work/w1"
  LC_ALL=C rgx -o '\w+' "$work/u8.txt" >"$work/w2"
  rgx -o '(?-u)\w+' "$work/u8.txt" >"$work/w3"
  printf 'unicode_word bytes=%s bytes_with_LC_ALL_C=%s ascii_only_bytes=%s\n' \
    "$(bytes "$work/w1")" "$(bytes "$work/w2")" "$(bytes "$work/w3")"

  printf 'foo\r\nbar\r\n' >"$work/crlf.txt"
  rgx -c 'foo$' "$work/crlf.txt" >"$work/c1"
  plain=$?
  rgx -c --crlf 'foo$' "$work/crlf.txt" >"$work/c2"
  crlf=$?
  # --crlf also makes rg end its own summary lines (here the count) in CR LF.
  tr -d '\r' <"$work/c2" >"$work/c3"
  if cmp -s "$work/c2" "$work/c3"; then cr=no; else cr=yes; fi
  printf 'crlf plain_status=%s plain_count=<%s> crlf_status=%s crlf_count=<%s> count_line_has_cr=%s\n' \
    "$plain" "$(joined "$work/c1")" "$crlf" "$(joined "$work/c3")" "$cr"

  printf 'foo\nbar\n' >"$work/m.txt"
  rgx 'foo\nbar' "$work/m.txt" >/dev/null 2>&1
  lit=$?
  rgx -U -c 'foo\nbar' "$work/m.txt" >"$work/m1"
  printf 'newline_in_pattern status=%s multiline_count=%s\n' \
    "$lit" "$(joined "$work/m1")"
}

# ancestor_vcs DIR: print yes if DIR or a parent holds .git or .jj, else no.
ancestor_vcs() {
  dir=$1
  while :; do
    if [ -e "$dir/.git" ] || [ -e "$dir/.jj" ]; then
      echo yes
      return
    fi
    [ "$dir" = / ] && break
    dir=$(dirname "$dir")
  done
  echo no
}

filter() {
  repo="$work/repo"
  mkdir -p "$repo/.git" "$repo/.hid" "$repo/sub"
  printf '*.skipme\n' >"$repo/.gitignore"
  for f in a.txt b.skipme .hid/h.txt sub/c.txt; do
    printf 'needle\n' >"$repo/$f"
  done
  printf 'needle\000bin\n' >"$repo/bin.dat"
  # --no-ignore-parent and --no-ignore-global keep the host's own ignore
  # files out of the result; --sort path makes the order deterministic.
  for u in none -u -uu -uuu; do
    if [ "$u" = none ]; then
      (cd "$repo" && rgx --no-ignore-parent --no-ignore-global --sort path -l needle) \
        </dev/null >"$work/l"
    else
      (cd "$repo" && rgx --no-ignore-parent --no-ignore-global --sort path "$u" -l needle) \
        </dev/null >"$work/l"
    fi
    printf 'list %s: %s\n' "$u" "$(joined "$work/l")"
  done
  (cd "$repo" && rgx -l needle .hid/h.txt) </dev/null >"$work/l"
  printf 'explicit_hidden: %s\n' "$(joined "$work/l")"
  (cd "$repo" && rgx --no-ignore-parent --no-ignore-global --sort path -l needle .) \
    </dev/null >"$work/l"
  printf 'explicit_dot: %s\n' "$(joined "$work/l")"

  plain="$work/plain"
  mkdir -p "$plain"
  printf '*.skipme\n' >"$plain/.gitignore"
  printf 'needle\n' >"$plain/a.txt"
  printf 'needle\n' >"$plain/b.skipme"
  (cd "$plain" && rgx --no-ignore-parent --no-ignore-global --sort path -l needle) \
    </dev/null >"$work/l1"
  (cd "$plain" && rgx --no-ignore-parent --no-ignore-global --no-require-git --sort path -l needle) \
    </dev/null >"$work/l2"
  printf 'gitignore_without_git ancestor_vcs=%s default: %s; no-require-git: %s\n' \
    "$(ancestor_vcs "$plain")" "$(joined "$work/l1")" "$(joined "$work/l2")"

  dotignore="$work/dotignore"
  mkdir -p "$dotignore"
  printf '*.skipme\n' >"$dotignore/.ignore"
  printf 'needle\n' >"$dotignore/a.txt"
  printf 'needle\n' >"$dotignore/b.skipme"
  (cd "$dotignore" && rgx --no-ignore-parent --no-ignore-global --sort path -l needle) \
    </dev/null >"$work/l"
  printf 'dot_ignore_without_git: %s\n' "$(joined "$work/l")"
}

binary() {
  mkdir -p "$work/early" "$work/late"
  printf 'needle\000bin\n' >"$work/early/early.dat"
  # 13 bytes, then 2000 lines of 40 bytes, so the NUL sits at offset 80013,
  # past the first 64 KiB read of the line buffer.
  {
    printf 'needle early\n'
    awk 'BEGIN { for (i = 0; i < 2000; i++) printf "%039d\n", i }'
    printf '\000needle late\n'
  } >"$work/late/late.dat"
  printf 'late_size=%s\n' "$(bytes "$work/late/late.dat")"

  (cd "$work/early" && rgx --no-ignore needle) </dev/null >"$work/out"
  st=$?
  printf 'walked_early status=%s stdout_bytes=%s\n' "$st" "$(bytes "$work/out")"
  (cd "$work/early" && rgx needle early.dat) </dev/null >"$work/out"
  st=$?
  printf 'explicit_early status=%s stdout=%s\n' "$st" "$(joined "$work/out")"

  for flags in default -c -l --binary; do
    if [ "$flags" = default ]; then
      (cd "$work/late" && rgx --no-ignore needle) </dev/null >"$work/out" 2>/dev/null
    else
      (cd "$work/late" && rgx --no-ignore "$flags" needle) </dev/null >"$work/out" 2>/dev/null
    fi
    st=$?
    printf 'walked_late %s status=%s\n' "$flags" "$st"
    while IFS= read -r line; do
      printf '  | %s\n' "$line"
    done <"$work/out"
  done
  (cd "$work/late" && rgx --no-ignore -a -c needle) </dev/null >"$work/out"
  printf 'walked_late -a -c: %s\n' "$(joined "$work/out")"
}

stdin_mode() {
  mkdir -p "$work/s"
  printf 'needle-in-file\n' >"$work/s/f.txt"
  printf 'needle-from-pipe\n' | (cd "$work/s" && rgx --no-ignore needle) >"$work/out"
  printf 'stdin_pipe: %s\n' "$(joined "$work/out")"
  (cd "$work/s" && rgx --no-ignore needle) </dev/null >"$work/out"
  printf 'stdin_dev_null: %s\n' "$(joined "$work/out")"
  (cd "$work/s" && rgx --no-ignore needle) <&- >"$work/out"
  printf 'stdin_closed: %s\n' "$(joined "$work/out")"
  (cd "$work/s" && rgx --no-ignore needle) <"$work/s/f.txt" >"$work/out"
  printf 'stdin_regular_file: %s\n' "$(joined "$work/out")"
  (cd "$work/s" && rgx --no-ignore needle .) </dev/null >"$work/out"
  printf 'explicit_dot: %s\n' "$(joined "$work/out")"

  # A bare rg inside a while-read loop inherits the loop's stdin, a regular
  # file, so it searches (and drains) the rest of the list.
  printf 'one\ntwo needle\nthree\n' >"$work/s/list"
  n=0
  while IFS= read -r item; do
    n=$((n + 1))
    : "$item"
    (cd "$work/s" && rgx --no-ignore needle) >/dev/null
  done <"$work/s/list"
  printf 'while_read_loop iterations=%s' "$n"
  n=0
  while IFS= read -r item; do
    n=$((n + 1))
    : "$item"
    (cd "$work/s" && rgx --no-ignore needle .) </dev/null >/dev/null
  done <"$work/s/list"
  printf ' with_path_and_dev_null=%s\n' "$n"
}

exit_mode() {
  printf 'needle\n' >"$work/f.txt"
  cd "$work" || exit 1
  rgx nomatch f.txt >/dev/null 2>&1
  printf 'rg no_match=%s' "$?"
  rgx needle f.txt missing.txt >/dev/null 2>&1
  printf ' match_and_error=%s' "$?"
  rgx -q needle missing.txt f.txt >/dev/null 2>&1
  printf ' quiet_match_and_error=%s' "$?"
  rgx '(' f.txt >/dev/null 2>&1
  printf ' bad_regex=%s\n' "$?"
  grep nomatch f.txt >/dev/null 2>&1
  printf 'grep no_match=%s' "$?"
  grep needle f.txt missing.txt >/dev/null 2>&1
  printf ' match_and_error=%s' "$?"
  grep -q needle missing.txt f.txt >/dev/null 2>&1
  printf ' quiet_match_and_error=%s' "$?"
  grep -E '(' f.txt >/dev/null 2>&1
  printf ' bad_regex=%s\n' "$?"
  mkdir -p "$work/hidden-only"
  printf 'x\n' >"$work/hidden-only/.h"
  (cd "$work/hidden-only" && rgx --no-ignore x) </dev/null >/dev/null 2>"$work/err"
  st=$?
  printf 'nothing_searched status=%s stderr_lines=%s\n' \
    "$st" "$(wc -l <"$work/err" | tr -d ' ')"
}

# into_fifo CMD...: run CMD with stdout on a FIFO whose reader, head -n 1,
# exits after one line; print CMD's exit status and the line head saw.
into_fifo() {
  rm -f "$work/fifo" "$work/head.out"
  mkfifo "$work/fifo"
  head -n 1 <"$work/fifo" >"$work/head.out" &
  reader=$!
  "$@" >"$work/fifo" 2>/dev/null
  st=$?
  wait "$reader"
  printf 'status=%s head_saw=<%s>' "$st" "$(joined "$work/head.out")"
}

pipe_mode() {
  mkdir -p "$work/d"
  awk 'BEGIN { for (i = 1; i <= 200000; i++) print "line", i }' >"$work/d/big.txt"
  printf 'output_bytes=%s\n' "$(bytes "$work/d/big.txt")"
  # Relative paths keep the printed file names short; -u keeps any ignore
  # file outside the scratch directory from hiding d/big.txt.
  cd "$work" || exit 1
  printf 'rg_one_file '
  into_fifo rgx line d/big.txt
  printf '\nrg_dir_j1 '
  into_fifo rgx -u -j1 line d
  printf '\nrg_dir_j2 '
  into_fifo rgx -u -j2 line d
  printf '\ngrep_one_file '
  into_fifo grep line d/big.txt
  printf '\n'
}

config_mode() {
  printf -- '--ignore-case\n' >"$work/rc"
  printf 'Needle\n' >"$work/n.txt"
  RIPGREP_CONFIG_PATH="$work/rc" rg -c needle "$work/n.txt" >"$work/out"
  st=$?
  printf 'config_ignore_case status=%s count=<%s>\n' "$st" "$(joined "$work/out")"
  RIPGREP_CONFIG_PATH="$work/rc" rg --no-config -c needle "$work/n.txt" >"$work/out"
  st=$?
  printf 'no_config status=%s count=<%s>\n' "$st" "$(joined "$work/out")"

  # A config file can name a preprocessor: plain "rg" then runs a program.
  # shellcheck disable=SC2016 # $RGLAB_MARKER belongs to the generated script
  printf '#!/bin/sh\n: >"$RGLAB_MARKER"\nexec cat\n' >"$work/pp.sh"
  chmod +x "$work/pp.sh"
  printf -- '--pre\n%s\n' "$work/pp.sh" >"$work/rc-pre"
  printf 'needle\n' >"$work/f.txt"
  RGLAB_MARKER="$work/marker" RIPGREP_CONFIG_PATH="$work/rc-pre" \
    rg -c needle "$work/f.txt" >"$work/out"
  if [ -e "$work/marker" ]; then ran=yes; else ran=no; fi
  printf 'config_pre ran=%s count=<%s>\n' "$ran" "$(joined "$work/out")"
  rm -f "$work/marker"
  RGLAB_MARKER="$work/marker" RIPGREP_CONFIG_PATH="$work/rc-pre" \
    rg --no-config -c needle "$work/f.txt" >"$work/out"
  if [ -e "$work/marker" ]; then ran=yes; else ran=no; fi
  printf 'no_config_pre ran=%s count=<%s>\n' "$ran" "$(joined "$work/out")"
}

case $mode in
  semantics) semantics ;;
  filter) filter ;;
  binary) binary ;;
  stdin) stdin_mode ;;
  exit) exit_mode ;;
  pipe) pipe_mode ;;
  config) config_mode ;;
  *)
    printf 'rg-probe: unknown mode: %s\n' "$mode" >&2
    exit 2
    ;;
esac
