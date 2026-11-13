#!/usr/bin/env bats
# Tests for mapfile and readarray: availability, every option, the read strategy (byte at a time
# on a pipe, buffered plus lseek back on a regular file), the callback, failure reporting, and the
# POSIX sh replacement. bash 4.0 to 4.3 arms follow the bash source of those releases.

setup() {
  load ../lib/lab
  export D="$BATS_TEST_TMPDIR"
  SCRIPT="$BATS_TEST_DIRNAME/read-records.sh"
}

# mapfile_tier: set TIER to "none" (no mapfile: bash 3.2 and every non-bash family), "4.0" to
# "4.3" for those bash releases, or "new" for bash 4.4 and later; fail on an unknown family.
mapfile_tier() {
  local family version
  family=$(lab_shell_family)
  case $family in
    bash)
      version=$(lab_shell_version)
      case $version in
        [123].*) TIER=none ;;
        4.[0-3].*) TIER=${version%"${version#4.?}"} ;;
        *) TIER=new ;;
      esac
      ;;
    zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash) TIER=none ;;
    *)
      printf 'unexpected shell family: %s\n' "$family" >&2
      return 1
      ;;
  esac
}

# assert_lacks_mapfile WANT: the shell under test has no mapfile, so it must not print WANT, and
# it must either name the missing command in a diagnostic or fail outright (a syntax error).
assert_lacks_mapfile() {
  [ "$output" != "$1" ]
  [[ $output == *mapfile* || $output == *readarray* ]] || [ "$status" -ne 0 ]
}

@test "mapfile and readarray are builtins of bash 4.0 and later only, also in POSIX mode" {
  mapfile_tier
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'for n in mapfile readarray; do
  if command -v "$n" >/dev/null 2>&1; then printf "%s " "$n"; fi
done
if [ -n "${BASH_VERSION-}" ]; then set -o posix; type -t mapfile; fi
echo end'
  [ "$status" -eq 0 ]
  case $TIER in
    none) [ "$output" = end ] ;;
    4.[0-3] | new) [ "$output" = "mapfile readarray builtin
end" ] ;;
    *) false ;;
  esac
}

@test "-t removes one trailing newline; readarray is the same builtin; MAPFILE is the default" {
  mapfile_tier
  printf 'a\n\nbc\n' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'mapfile x <"$D/f"
printf "%s " "${#x[0]}" "${#x[1]}" "${#x[2]}" "${#x[@]}"
mapfile -t x <"$D/f"
printf "%s " "${#x[0]}" "${#x[1]}" "${#x[2]}" "${#x[@]}"
readarray -t <"$D/f"
printf "<%s>" "${MAPFILE[@]}"'
  want='2 1 3 3 1 0 2 3 <a><><bc>'
  case $TIER in
    none) assert_lacks_mapfile "$want" ;;
    4.[0-3] | new)
      [ "$status" -eq 0 ]
      [ "$output" = "$want" ]
      ;;
    *) false ;;
  esac
}

@test "mapfile keeps a final line that has no newline" {
  mapfile_tier
  printf 'a\nb' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'mapfile -t x <"$D/f"; printf "<%s>" "${x[@]}"; echo " ${#x[@]}"'
  want='<a><b> 2'
  case $TIER in
    none) assert_lacks_mapfile "$want" ;;
    4.[0-3] | new)
      [ "$status" -eq 0 ]
      [ "$output" = "$want" ]
      ;;
    *) false ;;
  esac
}

@test "a plain while-read loop drops a final line without a newline in every shell" {
  printf 'a\nb' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'n=0
while IFS= read -r l; do n=$((n + 1)); done <"$D/f"
echo "$n [$l]"
n=0
while IFS= read -r l || [ -n "$l" ]; do n=$((n + 1)); done <"$D/f"
echo "$n"'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -eq 0 ]
      [ "$output" = "1 [b]
2" ]
      ;;
    *) false ;;
  esac
}

@test "the POSIX sh replacement for mapfile -t collects lines in the positional parameters" {
  printf 'a\n\n b \nlast' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'set --
while IFS= read -r l || [ -n "$l" ]; do set -- "$@" "$l"; done <"$D/f"
printf "%s:" "$#"; printf "<%s>" "$@"; echo'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      [ "$status" -eq 0 ]
      [ "$output" = "4:<a><>< b ><last>" ]
      ;;
    *) false ;;
  esac
}

@test "a NUL byte ends the element at the NUL, while read drops only the NUL" {
  mapfile_tier
  printf 'ab\0cd\nef\n' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'mapfile -t x <"$D/f"; printf "<%s>" "${x[@]}"; IFS= read -r l <"$D/f"; echo " <$l>"'
  want='<ab><ef> <abcd>'
  case $TIER in
    none) assert_lacks_mapfile "$want" ;;
    4.[3] | new)
      [ "$status" -eq 0 ]
      [ "$output" = "$want" ]
      ;;
    4.[0-2])
      # read skips NUL bytes only from bash 4.3 (CHANGES, bash-4.3-alpha item p)
      [ "$status" -eq 0 ]
      [[ $output == "<ab><ef> "* ]]
      ;;
    *) false ;;
  esac
}

@test "-d '' splits on NUL, keeps newlines inside records and an unterminated last record" {
  mapfile_tier
  printf 'a b\0c\nd\0e' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'mapfile -t -d "" x <"$D/f"; printf "<%s>" "${x[@]}"; echo " ${#x[@]}"'
  want='<a b><c
d><e> 3'
  case $TIER in
    none) assert_lacks_mapfile "$want" ;;
    new)
      [ "$status" -eq 0 ]
      [ "$output" = "$want" ]
      ;;
    4.[0-3])
      # -d arrived in bash 4.4 (NEWS, bash-4.4 item d)
      [[ $output == *"-d: invalid option"* ]]
      [ "${lines[${#lines[@]} - 1]}" = "<> 0" ]
      ;;
    *) false ;;
  esac
}

@test "-d uses only the first character of its argument" {
  mapfile_tier
  printf 'a:b;c:d' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'mapfile -t -d ":;" x <"$D/f"; printf "<%s>" "${x[@]}"; echo " ${#x[@]}"'
  want='<a><b;c><d> 3'
  case $TIER in
    none) assert_lacks_mapfile "$want" ;;
    new)
      [ "$status" -eq 0 ]
      [ "$output" = "$want" ]
      ;;
    4.[0-3])
      [[ $output == *"-d: invalid option"* ]]
      [ "${lines[${#lines[@]} - 1]}" = "<> 0" ]
      ;;
    *) false ;;
  esac
}

@test "-n COUNT on a regular file seeks back, so the next reader starts after COUNT lines" {
  mapfile_tier
  printf 'a\nb\nc\n' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c '{ mapfile -t -n 1 x; printf "<%s>" "${x[@]}"; /bin/cat; } <"$D/f"'
  case $TIER in
    none) assert_lacks_mapfile '<a>b
c' ;;
    4.[3] | new)
      [ "$status" -eq 0 ]
      [ "$output" = '<a>b
c' ]
      ;;
    4.[0-2])
      # before bash 4.3 the loop read one line past COUNT (CHANGES, bash-4.3-alpha item mmm)
      [ "$status" -eq 0 ]
      [ "$output" = '<a>c' ]
      ;;
    *) false ;;
  esac
}

@test "-n COUNT on a pipe reads one byte at a time, so nothing past COUNT lines is consumed" {
  mapfile_tier
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'printf "a\nb\nc\n" | { mapfile -t -n 1 x; printf "<%s>" "${x[@]}"; /bin/cat; }'
  case $TIER in
    none) assert_lacks_mapfile '<a>b
c' ;;
    4.[3] | new)
      [ "$status" -eq 0 ]
      [ "$output" = '<a>b
c' ]
      ;;
    4.[0-2])
      [ "$status" -eq 0 ]
      [ "$output" = '<a>c' ]
      ;;
    *) false ;;
  esac
}

@test "-O keeps elements outside the new range, the default clears, -s skips lines" {
  mapfile_tier
  printf 'a\nb' >"$D/two"
  printf 'z\n' >"$D/one"
  printf 'a\nb\nc\n' >"$D/three"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'x=(o1 o2 o3 o4)
mapfile -t -O 1 x <"$D/two"; echo "${x[*]}"
mapfile -t x <"$D/one"; echo "${x[*]}"
mapfile -t -s 1 -n 1 y <"$D/three"; echo "${y[*]}"'
  want='o1 a b o4
z
b'
  case $TIER in
    none) assert_lacks_mapfile "$want" ;;
    4.[0-3] | new)
      [ "$status" -eq 0 ]
      [ "$output" = "$want" ]
      ;;
    *) false ;;
  esac
}

@test "-O takes an unsigned 32-bit index that wraps to 0, and a larger origin is rejected" {
  mapfile_tier
  printf 'a\nb\nc\n' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'mapfile -t -O 4294967295 x <"$D/f"
echo "${!x[@]}"
printf "<%s>" "${x[@]}"; echo
mapfile -O 4294967296 y <"$D/f"
echo "status $?"'
  case $TIER in
    none) assert_lacks_mapfile '' ;;
    4.[0-3] | new)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "0 1 4294967295" ]
      [ "${lines[1]}" = "<b><c><a>" ]
      [[ $output == *"4294967296: invalid array origin"* ]]
      [ "${lines[${#lines[@]} - 1]}" = "status 1" ]
      ;;
    *) false ;;
  esac
}

@test "the callback runs every QUANTUM lines (default 5000) before the element is assigned" {
  mapfile_tier
  printf 'a\nb\nc\nd\ne\n' >"$D/five"
  awk 'BEGIN { for (i = 1; i <= 5001; i++) print "line" i }' >"$D/many"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'cb() { printf "cb %s %s n=%s\n" "$1" "$2" "${#x[@]}"; }
mapfile -t -C cb -c 2 x <"$D/five"; echo "${#x[@]}"
mapfile -t -C cb x <"$D/many"; echo "${#x[@]}"'
  case $TIER in
    none) assert_lacks_mapfile '' ;;
    4.[2-3] | new)
      [ "$status" -eq 0 ]
      [ "$output" = "cb 1 b n=1
cb 3 d n=3
5
cb 4999 line5000 n=4999
5001" ]
      ;;
    4.[0-1])
      # the line argument arrived in bash 4.2 (NEWS, bash-4.2 item l)
      [ "$status" -eq 0 ]
      [ "$output" = "cb 1  n=1
cb 3  n=3
5
cb 4999  n=4999
5001" ]
      ;;
    *) false ;;
  esac
}

@test "the callback gets the line single-quoted, so data cannot run, unless the callback is eval" {
  mapfile_tier
  export HIT="$D/hit"
  # shellcheck disable=SC2016 # the payloads are literal text
  printf '%s\n' '$(: >"$HIT")' "'; : >\"\$HIT\"; '" >"$D/payload"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'cb() { printf "<%s>\n" "$2"; }
mapfile -t -C cb -c 1 x <"$D/payload"
if [ -e "$HIT" ]; then echo created; else echo absent; fi'
  case $TIER in
    none) assert_lacks_mapfile '' ;;
    4.[2-3] | new)
      [ "$status" -eq 0 ]
      # shellcheck disable=SC2016 # literal payload text
      [ "${lines[0]}" = '<$(: >"$HIT")>' ]
      [ "${lines[1]}" = "<'; : >\"\$HIT\"; '>" ]
      [ "${lines[2]}" = absent ]
      ;;
    4.[0-1])
      [ "$status" -eq 0 ]
      [ "${lines[2]}" = absent ]
      ;;
    *) false ;;
  esac
  [ ! -e "$HIT" ]
  # shellcheck disable=SC2016 # the payload is literal text
  printf '%s\n' ': >"$HIT"' >"$D/payload"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'mapfile -t -C eval -c 1 x <"$D/payload"
if [ -e "$HIT" ]; then echo created; else echo absent; fi'
  case $TIER in
    none) assert_lacks_mapfile created ;;
    4.[2-3] | new) [ "${lines[${#lines[@]} - 1]}" = created ] ;;
    4.[0-1]) [ "${lines[${#lines[@]} - 1]}" = absent ] ;;
    *) false ;;
  esac
}

@test "mapfile returns 0 when the producer fails; wait on \$! recovers its status in bash 4.4+" {
  mapfile_tier
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'mapfile -t x < <(printf "a\n"; exit 3); s=$?; wait "$!"; echo "$s $? ${#x[@]}"'
  case $TIER in
    none) assert_lacks_mapfile '0 3 1' ;;
    new)
      [ "$status" -eq 0 ]
      [ "$output" = "0 3 1" ]
      ;;
    4.[0-3])
      # waiting for a process substitution arrived in bash 4.4 (NEWS, bash-4.4 item u)
      [ "${lines[${#lines[@]} - 1]}" = "0 127 1" ]
      ;;
    *) false ;;
  esac
}

@test "a read error ends mapfile silently with status 0, where read reports it" {
  mapfile_tier
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'mapfile -t x <"$D"; echo "mapfile=$? n=${#x[@]}"; IFS= read -r l <"$D"; echo "read=$?"'
  case $TIER in
    none) assert_lacks_mapfile '' ;;
    4.[0-3] | new)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "mapfile=0 n=0" ]
      [[ $output == *"read error"* ]]
      [ "${lines[${#lines[@]} - 1]}" = "read=1" ]
      ;;
    *) false ;;
  esac
}

@test "mapfile at the end of a pipeline fills a subshell's array, unless lastpipe is set" {
  mapfile_tier
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'printf "a\nb\n" | mapfile -t x; echo "${#x[@]}"
shopt -s lastpipe; printf "a\nb\n" | mapfile -t x; echo "${#x[@]}"'
  case $TIER in
    none) assert_lacks_mapfile '' ;;
    4.[2-3] | new)
      [ "$status" -eq 0 ]
      [ "$output" = "0
2" ]
      ;;
    4.[0-1])
      # lastpipe arrived in bash 4.2 (NEWS, bash-4.2 item t)
      [ "${lines[0]}" = 0 ]
      [ "${lines[${#lines[@]} - 1]}" = 0 ]
      ;;
    *) false ;;
  esac
}

@test "-u reads a coprocess through a duplicated descriptor that survives the coprocess" {
  mapfile_tier
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'coproc UP { tr a-z A-Z; }
exec {rd}<&"${UP[0]}"
wr=${UP[1]}
printf "a\nb\n" >&"$wr"
exec {wr}>&-
mapfile -t -u "$rd" x
exec {rd}<&-
wait
printf "<%s>" "${x[@]}"; echo'
  case $TIER in
    none) assert_lacks_mapfile '<A><B>' ;;
    4.[1-3] | new)
      [ "$status" -eq 0 ]
      [ "$output" = "<A><B>" ]
      ;;
    4.0)
      # {varname} redirections arrived in bash 4.1
      [ "$output" != "<A><B>" ]
      ;;
    *) false ;;
  esac
}

@test "an unusable target fails before any input is read" {
  mapfile_tier
  printf 'a\n' >"$D/f"
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'declare -A h; mapfile h <"$D/f"; a=$?
readonly r; mapfile r <"$D/f"; b=$?
mapfile -u 57 z; c=$?
mapfile "" <"$D/f"; d=$?
mapfile 1x <"$D/f"; e=$?
echo "$a $b $c $d $e"'
  case $TIER in
    none) assert_lacks_mapfile '1 1 1 2 1' ;;
    4.[0-3] | new)
      [ "$status" -eq 0 ]
      [[ $output == *"h: not an indexed array"* ]]
      [[ $output == *"r: readonly variable"* ]]
      [[ $output == *"57: invalid file descriptor"* ]]
      [ "${lines[${#lines[@]} - 1]}" = "1 1 1 2 1" ]
      ;;
    *) false ;;
  esac
}

@test "read-records.sh stores NUL-terminated records, newlines included" {
  mapfile_tier
  run_lab "$SCRIPT" printf 'a b\0c\nd\0e'
  case $TIER in
    new)
      [ "$status" -eq 0 ]
      [ "$output" = "3 records
<a b>
<c
d>
<e>" ]
      ;;
    none | 4.[0-3])
      case $(lab_shell_family) in
        bash | zsh | zsh-sh | yash | busybox | ksh93 | dash)
          [ "$status" -eq 2 ]
          [ "$output" = "read-records.sh: needs bash 4.4 or later" ]
          ;;
        mksh | posh)
          # these may reject the whole file at parse time (no process substitution)
          [ "$status" -ne 0 ]
          [[ $output != *records* ]]
          ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "read-records.sh exits with the producer's status, which mapfile alone would lose" {
  mapfile_tier
  run_lab "$SCRIPT" sh -c 'printf "x\0y"; exit 3'
  case $TIER in
    new)
      [ "$status" -eq 3 ]
      [ "$output" = "2 records
<x>
<y>" ]
      ;;
    none | 4.[0-3])
      [ "$status" -ne 0 ]
      [ "$status" -ne 3 ]
      [[ $output != *"2 records"* ]]
      ;;
    *) false ;;
  esac
}
