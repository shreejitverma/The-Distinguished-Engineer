#!/usr/bin/env bats
# Tests for the UTF-8 lab: utf8probe.c (what the C library decodes), utf8-validate.sh (RFC 3629 in awk),
# bidi-scan.sh (Trojan Source guard), shell-probe.sh (what the shell under test counts) and utf8-tour.sh.
# The shell under test never changes which cut, tr, sed, grep, wc or awk runs, so utility results branch on
# the implementation (GNU, BSD and macOS, BusyBox, uutils, mawk), not on lab_shell_family.
# Where an implementation was not measured when this file was written, its arm accepts the two values
# a conforming implementation can print (bytes or characters) instead of guessing one.

# shellcheck disable=SC2312,SC2016 # assertions read command substitutions on purpose; probe code is single-quoted for the shell under test

setup_file() {
  PROBE="$BATS_FILE_TMPDIR/utf8probe"
  "${CC:-cc}" -O1 -Wall -Wextra -Werror -o "$PROBE" "$BATS_TEST_DIRNAME/utf8probe.c"
}

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
  PROBE="$BATS_FILE_TMPDIR/utf8probe"
  VALIDATE="$LAB/utf8-validate.sh"
  BIDI="$LAB/bidi-scan.sh"
  SHPROBE="$LAB/shell-probe.sh"
  TOUR="$LAB/utf8-tour.sh"
  E=$(printf '\303\251')
  CAFE="caf$E"
}

# hex_file HEX FILE: write the bytes named by a hex string (bash printf understands \xHH).
hex_file() {
  local hex=$1 i
  : >"$2"
  for ((i = 0; i < ${#hex}; i += 2)); do
    # shellcheck disable=SC2059 # the format is the escape being built
    printf "\\x${hex:i:2}" >>"$2"
  done
}

# tool_version TOOL: the dotted version of a GNU tool (for example 9.12), or nothing.
tool_version() {
  local out
  out=$("$1" --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*)
      out=${out%%$'\n'*}
      printf '%s\n' "${out##* }"
      ;;
    *) ;;
  esac
}

# at_least VERSION MAJOR MINOR: true when a dotted version is >= MAJOR.MINOR.
at_least() {
  local maj=${1%%.*} min
  min=${1#*.}
  min=${min%%.*}
  [ "$maj" -gt "$2" ] || { [ "$maj" -eq "$2" ] && [ "$min" -ge "$3" ]; }
}

# flavor TOOL: gnu, bsd, busybox, uutils, or unknown, from the tool's own --version diagnostics.
flavor() {
  local out
  out=$("$1" --version 2>&1 </dev/null) || true
  case $out in
    *uutils*) echo uutils ;;
    *"not GNU sed"*) echo busybox ;;
    *BusyBox*) echo busybox ;;
    *"BSD grep"*) echo bsd ;;
    *GNU*) echo gnu ;;
    *usage:*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# hexs: the bytes on standard input as space-separated hex pairs on one line.
hexs() {
  od -An -v -tx1 | awk '{ for (i = 1; i <= NF; i++) printf "%s%s", (n++ ? " " : ""), $i } END { print "" }'
}

# keyval KEY: the value of a key=value line in $output.
keyval() {
  local line
  for line in "${lines[@]}"; do
    case $line in
      "$1="*)
        printf '%s\n' "${line#*=}"
        return 0
        ;;
      *) ;;
    esac
  done
  return 1
}

# libc: print glibc, apple, or other, as reported by the probe built against this host's C library.
libc() {
  local out
  out=$("$PROBE" mbmax C.UTF-8)
  out=${out#libc=}
  printf '%s\n' "${out%% *}"
}

decode() {
  "$PROBE" decode C.UTF-8 "$1"
}

@test "mbrtowc decodes sequences of 1 to 4 bytes at every length boundary" {
  run decode 7fc280dfbfe0a080efbfbff0908080f48fbfbf
  [ "$status" -eq 0 ]
  [ "$output" = "U+007F/1 U+0080/2 U+07FF/2 U+0800/3 U+FFFF/3 U+10000/4 U+10FFFF/4" ]
  # the two code points on either side of the surrogate gap D800..DFFF are ordinary characters
  run decode ed9fbfee8080
  [ "$output" = "U+D7FF/3 U+E000/3" ]
  run decode 41c3a9e282acf09f9880
  [ "$output" = "U+0041/1 U+00E9/2 U+20AC/3 U+1F600/4" ]
}

@test "mbrtowc rejects overlong forms, surrogates, bad lead bytes and lone continuation bytes" {
  local case hex want
  for case in \
    "c0af:bad bad" \
    "c1bf:bad bad" \
    "e08080:bad bad bad" \
    "eda080:bad bad bad" \
    "80:bad" \
    "ff:bad" \
    "fe:bad" \
    "c328:bad U+0028/1" \
    "41c342:U+0041/1 bad U+0042/1"; do
    hex=${case%%:*}
    want=${case#*:}
    run decode "$hex"
    [ "$status" -eq 0 ]
    [ "$output" = "$want" ] || {
      echo "decode $hex: got '$output', want '$want'" >&2
      false
    }
  done
}

@test "a sequence cut off at the end of input is short, not bad" {
  run decode e282
  [ "$output" = "short" ]
  run decode f09f98
  [ "$output" = "short" ]
  run decode 41e282
  [ "$output" = "U+0041/1 short" ]
}

@test "beyond U+10FFFF glibc decodes (gconv_simple.c utf8_internal_loop), Apple libc and musl reject" {
  local four five
  run decode f4908080
  four=$output
  run decode f888808080
  five=$output
  case $(libc) in
    glibc)
      [ "$four" = "U+110000/4" ]
      [ "$five" = "U+200000/5" ]
      ;;
    apple | other)
      [ "$four" = "bad bad bad bad" ]
      [ "$five" = "bad bad bad bad bad" ]
      ;;
    *) false ;;
  esac
}

@test "the C locale has one byte per character and wcwidth separates 0, 1 and 2 column characters" {
  run "$PROBE" mbmax C
  [ "$status" -eq 0 ]
  [[ $output == *"mb_cur_max=1" ]]
  run "$PROBE" decode C 41c3a9
  [ "$status" -eq 0 ]
  # three bytes, three characters; the exact code point values of bytes above 0x7F are libc specific
  [ "$(printf '%s\n' "$output" | wc -w | tr -d ' ')" -eq 3 ]
  run "$PROBE" width C.UTF-8 41 e9 301 4e2d 1f600
  [ "$output" = "U+0041=1 U+00E9=1 U+0301=0 U+4E2D=2 U+1F600=2" ]
  run "$PROBE" width C.UTF-8 zz
  [ "$status" -eq 2 ]
  run "$PROBE" mbmax no_SUCH.UTF-8
  [ "$status" -eq 2 ]
}

@test "the awk validator applies the RFC 3629 table and reports the first bad offset" {
  local row hex want want_rc file
  file="$BATS_TEST_TMPDIR/v.bin"
  for row in \
    "41:0:ok 1 bytes" \
    "c3a9:0:ok 2 bytes" \
    "e282ac:0:ok 3 bytes" \
    "f09f9880:0:ok 4 bytes" \
    "f48fbfbf:0:ok 4 bytes" \
    "efbfbf:0:ok 3 bytes" \
    "ed9fbf:0:ok 3 bytes" \
    "ee8080:0:ok 3 bytes" \
    "c0af:1:invalid at byte offset 0: byte 192 cannot start a sequence" \
    "c1bf:1:invalid at byte offset 0: byte 193 cannot start a sequence" \
    "e08080:1:invalid at byte offset 1: byte 128 is not a valid continuation here" \
    "eda080:1:invalid at byte offset 1: byte 160 is not a valid continuation here" \
    "f4908080:1:invalid at byte offset 1: byte 144 is not a valid continuation here" \
    "f8888080:1:invalid at byte offset 0: byte 248 cannot start a sequence" \
    "80:1:invalid at byte offset 0: byte 128 cannot start a sequence" \
    "ff:1:invalid at byte offset 0: byte 255 cannot start a sequence" \
    "e282:1:invalid at byte offset 2: truncated sequence at end of input" \
    "f09f98:1:invalid at byte offset 3: truncated sequence at end of input" \
    "c328:1:invalid at byte offset 1: byte 40 is not a valid continuation here" \
    "41c342:1:invalid at byte offset 2: byte 66 is not a valid continuation here"; do
    hex=${row%%:*}
    row=${row#*:}
    want_rc=${row%%:*}
    want=${row#*:}
    hex_file "$hex" "$file"
    run_lab "$VALIDATE" "$file"
    [ "$status" -eq "$want_rc" ]
    [ "$output" = "$want" ] || {
      echo "validate $hex: got '$output', want '$want'" >&2
      false
    }
  done
}

@test "the validator's exit status is 0 for valid input and 1 for invalid input, 2 for a usage error" {
  local file="$BATS_TEST_TMPDIR/v.bin"
  hex_file c3a9 "$file"
  run_lab "$VALIDATE" "$file"
  [ "$status" -eq 0 ]
  hex_file c3 "$file"
  run_lab "$VALIDATE" "$file"
  [ "$status" -eq 1 ]
  run_lab "$VALIDATE" "$BATS_TEST_TMPDIR/missing"
  [ "$status" -eq 2 ]
  run_lab "$VALIDATE" a b
  [ "$status" -eq 2 ]
  # standard input and an empty file are valid, and the validator is independent of the locale
  run_lab_c 'printf "" | LC_ALL=C "$0"' "$VALIDATE"
  [ "$status" -eq 0 ]
  [ "$output" = "ok 0 bytes" ]
}

@test "libc and the awk validator agree on every case where RFC 3629 and the C library both rule" {
  local hex file out libc_bad
  file="$BATS_TEST_TMPDIR/v.bin"
  for hex in 41 c3a9 e282ac f09f9880 f48fbfbf efbfbf ed9fbf ee8080 \
    c0af c1bf e08080 eda080 f8888080 80 ff e282 f09f98 c328 41c342; do
    hex_file "$hex" "$file"
    out=$(decode "$hex")
    libc_bad=0
    case $out in
      *bad* | *short*) libc_bad=1 ;;
      *) ;;
    esac
    run_lab "$VALIDATE" "$file"
    if [ "$libc_bad" -eq 1 ]; then
      [ "$status" -eq 1 ]
    else
      [ "$status" -eq 0 ]
    fi
  done
  # F4 90 80 80 is U+110000: the validator rejects it on every host, glibc's decoder accepts it
  hex_file f4908080 "$file"
  run_lab "$VALIDATE" "$file"
  [ "$status" -eq 1 ]
}

@test "a shell counts characters or bytes in \${#x} according to its family, and bytes in the C locale" {
  local len dq pad prec family
  family=$(lab_shell_family)
  export LC_ALL=C
  run_lab "$SHPROBE"
  [ "$status" -eq 0 ]
  [ "$(keyval locale)" = C ]
  [ "$(keyval len)" = 5 ]
  [ "$(keyval pad)" = bytes ]
  [ "$(keyval prec)" = bytes ]
  export LC_ALL=C.UTF-8
  run_lab "$SHPROBE"
  [ "$status" -eq 0 ]
  [ "$(keyval locale)" = C.UTF-8 ]
  len=$(keyval len)
  dq=$(keyval dq)
  pad=$(keyval pad)
  prec=$(keyval prec)
  case $family in
    bash)
      [ "$len" = 4 ]
      [ "$dq" = yes ]
      [ "$pad" = bytes ]
      [ "$prec" = bytes ]
      ;;
    zsh | zsh-sh)
      [ "$len" = 4 ]
      [ "$pad" = chars ]
      [ "$prec" = chars ]
      ;;
    dash)
      # dash 0.5.13 added multibyte support (git log: "expand: Count multi-byte characters for VSLENGTH")
      # together with the dollar-single-quote form, so that form tells the two generations apart.
      if [ "$dq" = yes ]; then
        [ "$len" = 4 ]
      else
        [ "$len" = 5 ]
      fi
      [ "$pad" = bytes ]
      [ "$prec" = bytes ]
      ;;
    busybox)
      [ "$len" = 4 ]
      [ "$pad" = bytes ]
      [ "$prec" = bytes ]
      ;;
    ksh93)
      [ "$len" = 4 ]
      case $(lab_shell_version) in
        *93u+m*) [ "$pad" = bytes ] || [ "$pad" = chars ] ;;
        *) [ "$pad" = bytes ] ;;
      esac
      ;;
    mksh)
      # mksh decides by its own utf8-mode, which "set -U" and "set +U" switch explicitly.
      [ "$len" = 4 ] || [ "$len" = 5 ]
      run_lab_c 'set -U; x=$(printf "caf\303\251"); echo ${#x}'
      [ "$output" = 4 ]
      run_lab_c 'set +U; x=$(printf "caf\303\251"); echo ${#x}'
      [ "$output" = 5 ]
      ;;
    yash)
      [ "$len" = 4 ] || [ "$len" = 5 ]
      [ "$pad" = bytes ] || [ "$pad" = chars ]
      ;;
    posh)
      [ "$len" = 4 ] || [ "$len" = 5 ]
      [ "$pad" = bytes ] || [ "$pad" = chars ]
      ;;
    *) false ;;
  esac
}

@test "cut -d with an ASCII delimiter is safe on UTF-8 in any locale because ASCII bytes never occur inside a sequence" {
  local line
  line=$(printf '%s,\344\270\255,x' "$E")
  local c u
  c=$(printf '%s\n' "$line" | LC_ALL=C cut -d, -f2 | hexs)
  u=$(printf '%s\n' "$line" | LC_ALL=C.UTF-8 cut -d, -f2 | hexs)
  [ "$c" = "e4 b8 ad 0a" ]
  [ "$u" = "e4 b8 ad 0a" ]
}

@test "cut -c and cut -b: GNU 9.11 and later, BSD and uutils select characters, older GNU and BusyBox bytes" {
  local c b nb v
  c=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 cut -c4 | hexs)
  b=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 cut -b4 | hexs)
  nb=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 cut -n -b4 | hexs)
  # -b4 is the first byte of U+00E9 in every implementation, because that is what -b means
  [ "$b" = "c3 0a" ]
  case $(flavor cut) in
    gnu)
      v=$(tool_version cut)
      if at_least "$v" 9 11; then
        [ "$c" = "c3 a9 0a" ]
        [ "$nb" = "0a" ]
      else
        [ "$c" = "c3 0a" ]
        [ "$nb" = "c3 0a" ]
      fi
      ;;
    bsd)
      [ "$c" = "c3 a9 0a" ]
      [ "$nb" = "0a" ]
      ;;
    busybox)
      [ "$c" = "c3 0a" ]
      [ "$nb" = "c3 0a" ]
      ;;
    uutils)
      [ "$c" = "c3 a9 0a" ]
      ;;
    *) false ;;
  esac
  # in the C locale every byte is a character, so -c4 is the first byte everywhere
  c=$(printf '%s\n' "$CAFE" | LC_ALL=C cut -c4 | hexs)
  [ "$c" = "c3 0a" ]
}

@test "tr maps bytes in GNU and BusyBox, characters in macOS tr, and bytes in every tr under LC_ALL=C" {
  local lower upper fixed
  lower=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 tr '[:lower:]' '[:upper:]' | hexs)
  upper=$(printf '%s\n' "$CAFE" | LC_ALL=C tr '[:lower:]' '[:upper:]' | hexs)
  fixed=$(printf 'caf\303\251\n' | LC_ALL=C.UTF-8 tr '\303\251' 'XY')
  # C locale: e-acute is two bytes that are not letters, so only the ASCII letters change
  [ "$upper" = "43 41 46 c3 a9 0a" ]
  case $(flavor tr) in
    gnu | busybox)
      # GNU manual: "GNU tr fully supports only safe single-byte locales"
      [ "$lower" = "43 41 46 c3 a9 0a" ]
      [ "$fixed" = cafXY ]
      ;;
    bsd)
      [ "$lower" = "43 41 46 c3 89 0a" ]
      ;;
    uutils)
      [ "$lower" = "43 41 46 c3 a9 0a" ] || [ "$lower" = "43 41 46 c3 89 0a" ]
      ;;
    *) false ;;
  esac
}

@test "sed dot matches one character in UTF-8 and one byte in the C locale; invalid bytes differ by implementation" {
  local utf8 cbytes bad
  utf8=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 sed 's/./x/g')
  cbytes=$(printf '%s\n' "$CAFE" | LC_ALL=C sed 's/./x/g')
  [ "$cbytes" = xxxxx ]
  case $(flavor sed) in
    gnu | bsd)
      [ "$utf8" = xxxx ]
      ;;
    busybox | uutils)
      [ "$utf8" = xxxx ] || [ "$utf8" = xxxxx ]
      ;;
    *) false ;;
  esac
  bad=$(printf 'a\377b\n' | LC_ALL=C.UTF-8 sed 's/./x/g' 2>/dev/null | hexs)
  case $(flavor sed) in
    gnu)
      # GNU sed manual: regular expressions do not match invalid multibyte sequences, so the byte survives
      [ "$bad" = "78 ff 78 0a" ]
      ;;
    bsd)
      # macOS sed stops with "illegal byte sequence" and prints nothing
      [ "$bad" = "" ]
      ;;
    busybox | uutils)
      [ "$bad" = "78 ff 78 0a" ] || [ "$bad" = "78 78 78 0a" ] || [ "$bad" = "" ]
      ;;
    *) false ;;
  esac
}

@test "awk length counts characters in gawk and bytes in mawk; every awk counts bytes in the C locale" {
  local awk flavor_name n c
  while IFS= read -r awk; do
    # shellcheck disable=SC2086 # "busybox awk" is a command plus an argument
    flavor_name=$(lab_awk_flavor $awk)
    # shellcheck disable=SC2086
    n=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 $awk '{ print length($0) }')
    # shellcheck disable=SC2086
    c=$(printf '%s\n' "$CAFE" | LC_ALL=C $awk '{ print length($0) }')
    [ "$c" = 5 ]
    case $flavor_name in
      gawk) [ "$n" = 4 ] ;;
      mawk) [ "$n" = 5 ] ;;
      onetrue | busybox | unknown) [ "$n" = 4 ] || [ "$n" = 5 ] ;;
      *) false ;;
    esac
  done < <(lab_awks)
}

@test "gawk toupper follows the locale; gawk -b and the C locale are byte-wise; U+00DF has no one-character uppercase" {
  local out awks
  awks=$(lab_awks)
  case $awks in
    *gawk*) ;;
    *)
      # no gawk on this host: the other awks have nothing locale-driven to assert here
      [ -n "$awks" ]
      return 0
      ;;
  esac
  out=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 gawk '{ print toupper($0) }' | hexs)
  [ "$out" = "43 41 46 c3 89 0a" ]
  out=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 gawk -b '{ print length($0) }')
  [ "$out" = 5 ]
  out=$(printf '%s\n' "$CAFE" | LC_ALL=C gawk '{ print toupper($0) }' | hexs)
  [ "$out" = "43 41 46 c3 a9 0a" ]
  # simple case mapping leaves the sharp s alone; the full mapping to "SS" is not what toupper does
  out=$(printf 'stra\303\237e\n' | LC_ALL=C.UTF-8 gawk '{ print toupper($0) }' | hexs)
  [ "$out" = "53 54 52 41 c3 9f 45 0a" ]
}

@test "wc -c counts bytes everywhere; wc -m counts characters in GNU and macOS wc and bytes in the C locale" {
  local c m cm
  c=$(printf %s "$CAFE" | wc -c | tr -d ' ')
  m=$(printf %s "$CAFE" | LC_ALL=C.UTF-8 wc -m | tr -d ' ')
  cm=$(printf %s "$CAFE" | LC_ALL=C wc -m | tr -d ' ')
  [ "$c" = 5 ]
  [ "$cm" = 5 ]
  case $(flavor wc) in
    gnu | bsd) [ "$m" = 4 ] ;;
    busybox | uutils) [ "$m" = 4 ] || [ "$m" = 5 ] ;;
    *) false ;;
  esac
}

@test "grep dot and bracket classes follow LC_CTYPE: bytes in C, characters in UTF-8" {
  local c u a ca
  c=$(printf '%s\n' "$CAFE" | LC_ALL=C grep -o . | wc -l | tr -d ' ')
  u=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 grep -o . | wc -l | tr -d ' ')
  a=$(printf '%s\n' "$CAFE" | LC_ALL=C.UTF-8 grep -c '^[[:alpha:]]*$') || true
  ca=$(printf '%s\n' "$CAFE" | LC_ALL=C grep -c '^[[:alpha:]]*$') || true
  [ "$c" = 5 ]
  [ "$ca" = 0 ]
  case $(flavor grep) in
    gnu | bsd)
      [ "$u" = 4 ]
      [ "$a" = 1 ]
      ;;
    busybox | uutils)
      [ "$u" = 4 ] || [ "$u" = 5 ]
      [ "$a" = 1 ] || [ "$a" = 0 ]
      ;;
    *) false ;;
  esac
}

@test "a UTF-8 BOM stays in the first field: cut keeps it, awk comparison fails, tail -c +4 strips it" {
  local file="$BATS_TEST_TMPDIR/bom.csv" f1 stripped awk hits
  printf '\357\273\277id,name\n1,x\n' >"$file"
  f1=$(cut -d, -f1 "$file" | head -n 1 | hexs)
  [ "$f1" = "ef bb bf 69 64 0a" ]
  while IFS= read -r awk; do
    # shellcheck disable=SC2086 # "busybox awk" is a command plus an argument
    # C.UTF-8 compares by code point; a language locale could collate the invisible BOM away on some awks
    hits=$(LC_ALL=C.UTF-8 $awk -F, '$1 == "id" { print "match" }' "$file")
    [ -z "$hits" ]
  done < <(lab_awks)
  stripped=$(tail -c +4 "$file" | cut -d, -f1 | head -n 1)
  [ "$stripped" = id ]
}

@test "a shell run on a script that starts with a BOM reports the first line as a command and carries on" {
  local plain="$BATS_TEST_TMPDIR/plain.sh" bom="$BATS_TEST_TMPDIR/bom.sh"
  printf '#!/bin/sh\necho ran\n' >"$plain"
  printf '\357\273\277#!/bin/sh\necho ran\n' >"$bom"
  run_lab "$plain"
  [ "$status" -eq 0 ]
  [ "$output" = ran ]
  run_lab "$bom"
  [ "$status" -eq 0 ]
  # the script still runs to its last line in every shell
  [ "${lines[${#lines[@]} - 1]}" = ran ]
  case $(lab_shell_family) in
    bash | dash | zsh | zsh-sh)
      # one diagnostic naming the BOM-prefixed word, then the second line's output
      [ "${#lines[@]}" -eq 2 ]
      [[ ${lines[0]} == *"#!/bin/sh"* ]]
      ;;
    ksh93)
      case $(lab_shell_version) in
        *93u+m*) [ "${#lines[@]}" -ge 1 ] ;;
        *)
          [ "${#lines[@]}" -eq 2 ]
          [[ ${lines[0]} == *"#!/bin/sh"* ]]
          ;;
      esac
      ;;
    busybox | mksh | yash | posh) [ "${#lines[@]}" -ge 1 ] ;;
    *) false ;;
  esac
}

@test "bidi-scan finds each of the nine bidirectional controls and nothing near them" {
  local file="$BATS_TEST_TMPDIR/code.txt" tail
  for tail in '\200\252' '\200\253' '\200\254' '\200\255' '\200\256' '\201\246' '\201\247' '\201\250' '\201\251'; do
    # shellcheck disable=SC2059 # the escapes are the bytes being built
    printf "ok\\nx = 1 \\342${tail}// y\\n" >"$file"
    run_lab "$BIDI" "$file"
    [ "$status" -eq 1 ]
    [ "$output" = "bidi control character on line 2" ]
  done
  # neighbours in the same blocks (U+2029, U+202F, U+2065, U+206A), a right single quote and the euro sign are not flagged
  for tail in '\200\251' '\200\257' '\201\245' '\201\252' '\200\231' '\202\254'; do
    # shellcheck disable=SC2059
    printf "ok\\nit \\342${tail} s\\n" >"$file"
    run_lab "$BIDI" "$file"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
  done
  printf '%s \344\270\255 \360\237\230\200\n' "$CAFE" >"$file"
  run_lab "$BIDI" "$file"
  [ "$status" -eq 0 ]
}

@test "bidi-scan reads standard input and is not hidden by an invalid byte on the same line" {
  run_lab_c 'printf "a\377b \342\200\256\nclean\n" | "$0"' "$BIDI"
  [ "$status" -eq 1 ]
  [ "$output" = "bidi control character on line 1" ]
  run_lab "$BIDI" "$BATS_TEST_TMPDIR/does-not-exist"
  [ "$status" -eq 2 ]
}

@test "the tour prints the byte, character, normalization, BOM, sort-order and bidi facts" {
  local i row
  run_lab "$TOUR" "$PROBE"
  [ "$status" -eq 0 ]
  i=0
  for row in \
    'bytes U+00E9|2' \
    'bytes U+20AC|3' \
    'bytes U+1F600|4' \
    'bytes cafe (NFC)|5' \
    'bytes cafe (NFD)|6' \
    'chars cafe (NFC)|4' \
    'chars cafe (NFD)|5' \
    'grep NFC in NFD text|0' \
    'sort -u NFC and NFD|2' \
    'BOM in first field|ef bb bf 69 64 0a' \
    'C sort = code points|7a 0a c3 a9 0a e2 82 ac 0a f0 9f 98 80 0a' \
    'lines with U+202E|1'; do
    [ "${lines[$i]}" = "$(printf '%-26s %s' "${row%%|*}" "${row#*|}")" ]
    i=$((i + 1))
  done
  [ "${#lines[@]}" -eq 12 ]
}
