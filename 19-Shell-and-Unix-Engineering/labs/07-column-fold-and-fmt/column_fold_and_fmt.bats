#!/usr/bin/env bats
# column_fold_and_fmt.bats - fold, fmt, and column behavior per implementation.
#
# The shell under test (LAB_SHELL) runs wrap_lab.sh, but it never changes which fold,
# fmt, or column runs, so every test loops over the implementations installed here
# (for fold: fold, gfold, busybox fold) and asserts the behavior of each one's flavor.
# Flavors: gnu (coreutils fold 9.8+ with multibyte support, fmt 9.10+ with an inclusive
# -w), gnu-bytes (coreutils fold before 9.8), gnu-pre910 (coreutils fmt before 9.10),
# bsd (macOS and FreeBSD), busybox, util-linux (column).
# A flavor a test has no expectation for, or a utility that is not installed, fails.

setup() {
  load ../lib/lab
  export TMPDIR="$BATS_TEST_TMPDIR"
  LAB="$BATS_TEST_DIRNAME/wrap_lab.sh"
}

# impls_of KIND: each installed implementation of KIND (fold, fmt, column), one per line.
impls_of() {
  local cmd
  for cmd in "$1" "g$1"; do
    if command -v "$cmd" >/dev/null 2>&1; then
      printf '%s\n' "$cmd"
    fi
  done
  if command -v busybox >/dev/null 2>&1 && busybox "$1" </dev/null >/dev/null 2>&1; then
    printf 'busybox %s\n' "$1"
  fi
}

# coreutils_at_least TEXT MAJOR MINOR: TEXT holds "(GNU coreutils) X.Y"; succeed if X.Y >= MAJOR.MINOR.
coreutils_at_least() {
  local ver major minor
  ver=${1#*coreutils) }
  ver=${ver%%[!0-9.]*}
  major=${ver%%.*}
  minor=${ver#*.}
  minor=${minor%%.*}
  [ "$major" -gt "$2" ] || { [ "$major" -eq "$2" ] && [ "$minor" -ge "$3" ]; }
}

# flavor_of KIND CMD [ARG...]: print the flavor of one implementation, detected from
# its own --version and --help text (BusyBox and BSD print usage for both).
flavor_of() {
  local kind=$1 text
  shift
  text="$("$@" --version 2>&1 </dev/null) $("$@" --help 2>&1 </dev/null)" || true
  case $kind:$text in
    *BusyBox*) echo busybox ;;
    *uutils*) echo uutils ;;
    fold:*"GNU coreutils"*)
      case $text in
        *--characters*) echo gnu ;;
        *) echo gnu-bytes ;;
      esac
      ;;
    fmt:*"GNU coreutils"*)
      if coreutils_at_least "$text" 9 10; then echo gnu; else echo gnu-pre910; fi
      ;;
    column:*util-linux*) echo util-linux ;;
    fold:*"usage: fold [-bs]"* | fmt:*"fmt [-cmps]"* | column:*"usage: column [-tx]"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# check KIND PROBE FLAVOR=EXPECTED...: run PROBE through wrap_lab.sh for every installed
# implementation of KIND and compare the output with the EXPECTED for its flavor.
check() {
  local kind=$1 probe=$2 impls impl flavor exp pair found
  local -a words
  shift 2
  impls=$(impls_of "$kind")
  if [ -z "$impls" ]; then
    echo "no $kind implementation is installed"
    return 1
  fi
  while IFS= read -r impl; do
    read -r -a words <<<"$impl"
    flavor=$(flavor_of "$kind" "${words[@]}")
    found=no
    for pair in "$@"; do
      if [ "${pair%%=*}" = "$flavor" ]; then
        exp=${pair#*=}
        found=yes
      fi
    done
    if [ "$found" = no ]; then
      echo "$impl: no expectation for flavor $flavor"
      return 1
    fi
    run_lab "$LAB" "$probe" "${words[@]}" </dev/null
    echo "$impl ($flavor): got [$output] want [$exp]"
    [ "$output" = "$exp" ]
  done <<<"$impls"
}

@test "fold -w 4 breaks ASCII input every 4 columns" {
  check fold fold-ascii \
    "gnu=abcd|efgh|ij| status=0" "gnu-bytes=abcd|efgh|ij| status=0" \
    "bsd=abcd|efgh|ij| status=0" "busybox=abcd|efgh|ij| status=0"
}

@test "fold -s breaks after the last blank and keeps that blank at the end of the segment" {
  check fold fold-spaces \
    "gnu=the quick |brown fox| status=0" "gnu-bytes=the quick |brown fox| status=0" \
    "bsd=the quick |brown fox| status=0" "busybox=the quick |brown fox| status=0"
}

@test "fold advances a tab to the next multiple of 8 columns" {
  check fold fold-tab \
    "gnu=a>bc|defghijk| status=0" "gnu-bytes=a>bc|defghijk| status=0" \
    "bsd=a>bc|defghijk| status=0" "busybox=a>bc|defghijk| status=0"
}

@test "fold resets the column count at a carriage return, so 13 bytes fit in -w 8" {
  check fold fold-cr \
    "gnu=abcdef^ghijkl| status=0" "gnu-bytes=abcdef^ghijkl| status=0" \
    "bsd=abcdef^ghijkl| status=0" "busybox=abcdef^ghijkl| status=0"
}

@test "fold adds no newline to a final line that had none" {
  check fold fold-noeol \
    "gnu=abc|def|g status=0" "gnu-bytes=abc|def|g status=0" \
    "bsd=abc|def|g status=0" "busybox=abc|def|g status=0"
}

@test "fold in a UTF-8 locale: multibyte-aware folds keep U+00E9 whole, coreutils before 9.8 splits it" {
  check fold fold-utf8 \
    "gnu=[][][]|[][][]| status=0" "gnu-bytes=[][|][]|[][|][]| status=0" \
    "bsd=[][][]|[][][]| status=0" "busybox=[][][]|[][][]| status=0"
}

@test "fold in the C locale: GNU and BusyBox split UTF-8 bytes, BSD gives them zero width and never folds" {
  check fold fold-c-locale \
    "gnu=[][|][]|[][|][]| status=0" "gnu-bytes=[][|][]|[][|][]| status=0" \
    "bsd=[][][][][][]| status=0" "busybox=[][|][]|[][|][]| status=0"
}

@test "fold on double-width U+4E2D: wcwidth-aware folds count 2 columns, BusyBox counts 1" {
  check fold fold-wide \
    "gnu={~}{~}|{~}| status=0" "gnu-bytes={~}{|~}{~|}| status=0" \
    "bsd={~}{~}|{~}| status=0" "busybox={~}{~}{~}| status=0"
}

@test "fold -b -w 1 on a 2-byte character: GNU 9.8 keeps it whole, BSD adds an empty line, others split it" {
  check fold fold-bytes \
    "gnu=[]|x| status=0" "gnu-bytes=[|]|x| status=0" \
    "bsd=|[]|x| status=0" "busybox=[|]|x| status=0"
}

@test "fold on an invalid UTF-8 byte: BSD stops at it and still exits 0" {
  check fold fold-invalid \
    "gnu=ab#cd|ef| status=0" "gnu-bytes=ab#cd|ef| status=0" \
    "bsd=ab status=0" "busybox=ab#cd|ef| status=0"
}

@test "fmt default widths: BSD fills greedily to goal 65, GNU optimizes toward goal 70 under max 75" {
  check fmt fmt-default \
    "gnu=71 59 status=0" "gnu-pre910=71 59 status=0" "bsd=62 68 status=0"
}

@test "fmt -w 9 on a 9-byte line: inclusive since coreutils 9.10, exclusive before it" {
  check fmt fmt-exact \
    "gnu=aaaa bbbb| status=0" "gnu-pre910=aaaa|bbbb| status=0" "bsd=aaaa bbbb| status=0"
}

@test "fmt on CRLF input: BSD strips the carriage returns, GNU keeps them as words inside the line" {
  check fmt fmt-crlf \
    "gnu=aa bb^ cc dd^| status=0" "gnu-pre910=aa bb^ cc dd^| status=0" \
    "bsd=aa bb cc dd| status=0"
}

@test "fmt -w 12 on UTF-8 words: BSD counts columns, GNU counts bytes" {
  check fmt fmt-utf8 \
    "gnu=[][][]|[][][]|[][][]|[][][]| status=0" \
    "gnu-pre910=[][][]|[][][]|[][][]|[][][]| status=0" \
    "bsd=[][][] [][][] [][][]|[][][]| status=0"
}

@test "fmt in the C locale: BSD silently deletes non-ASCII bytes, GNU passes them through" {
  check fmt fmt-c-locale \
    "gnu=caf[] ok| status=0" "gnu-pre910=caf[] ok| status=0" "bsd=caf ok| status=0"
}

@test "fmt keeps an 8-space indent: BSD rewrites it as a tab, GNU keeps spaces" {
  check fmt fmt-indent \
    "gnu=        aa bb cc dd| status=0" "gnu-pre910=        aa bb cc dd| status=0" \
    "bsd=>aa bb cc dd| status=0"
}

@test "fmt and a line starting with a dot: BSD leaves it alone, GNU joins it" {
  check fmt fmt-dot \
    "gnu=aa bb .TH x cc dd| status=0" "gnu-pre910=aa bb .TH x cc dd| status=0" \
    "bsd=aa bb|.TH x|cc dd| status=0"
}

@test "fmt puts two spaces after a sentence end it joins at a line break" {
  check fmt fmt-sentence \
    "gnu=One.  Two. Three.  Four| status=0" "gnu-pre910=One.  Two. Three.  Four| status=0" \
    "bsd=One.  Two. Three.  Four| status=0"
}

@test "column fills columns first, padding with tabs to 8-column stops" {
  check column column-fill \
    "bsd=a>d>g|b>e>h|c>f| status=0" "util-linux=a>d>g|b>e>h|c>f| status=0"
}

@test "column -x fills rows first" {
  check column column-fillrows \
    "bsd=a>b>c|d>e>f|g>h| status=0" "util-linux=a>b>c|d>e>f|g>h| status=0"
}

@test "column takes its width from COLUMNS when standard output is not a terminal" {
  check column column-env \
    "bsd=a>e|b>f|c>g|d>h| status=0" "util-linux=a>e|b>f|c>g|d>h| status=0"
}

@test "column -t aligns with two spaces and drops the empty line" {
  check column column-table \
    "bsd=name   size|alpha  1|b      22222| status=0" \
    "util-linux=name   size|alpha  1|b      22222| status=0"
}

@test "column -t -s , on an empty field: BSD merges adjacent separators, util-linux keeps the field" {
  check column column-empty-field \
    "bsd=a  b  c|1  3| status=0" "util-linux=a  b  c|1     3| status=0"
}

@test "column -t on a last line without a newline: BSD drops it and exits 1" {
  check column column-noeol \
    "bsd=a  b| status=1" "util-linux=a   b|cc  d| status=0"
}

@test "column -t on an invalid UTF-8 byte: BSD prints nothing and exits 0, util-linux escapes it" {
  check column column-invalid \
    "bsd= status=0" 'util-linux=a\xffb  x|c       y| status=0'
}

@test "column on a 3000-byte line: BSD keeps only the bytes after its 2048-character buffer and exits 1" {
  check column column-long-line \
    "bsd=952 5 status=1" "util-linux=3000 5 status=0"
}
