#!/usr/bin/env bats
# BRE, ERE, and PCRE: the same characters through grep, sed, expr, awk, and the C library's regcomp.
# The shell under test never changes which grep, sed, or libc runs, so utility results branch on
# the implementation (grep_flavor, sed_flavor, and the libc the probe was built against).

setup() {
  load ../lib/lab
  LAB="$BATS_TEST_DIRNAME"
}

# build_probe: compile regprobe.c into the test's scratch directory as $PROBE.
build_probe() {
  PROBE="$BATS_TEST_TMPDIR/regprobe"
  "${CC:-cc}" -Wall -Wextra -Werror -o "$PROBE" "$LAB/regprobe.c"
}

# probe ARG...: run regprobe under the shell under test.
probe() {
  # shellcheck disable=SC2016 # "$0" and "$@" belong to the shell under test
  run_lab_c 'exec "$0" "$@"' "$PROBE" "$@"
}

# grep_flavor: gnu, bsd (FreeBSD and macOS), busybox, or unknown, from grep's own banner.
grep_flavor() {
  local v
  v=$(grep --version 2>&1 </dev/null) || true
  case $v in
    *"GNU grep"*) echo gnu ;;
    *"BSD grep"*) echo bsd ;;
    *BusyBox*) echo busybox ;;
    *) echo unknown ;;
  esac
}

# sed_flavor: gnu, bsd, busybox, or unknown. BusyBox sed answers --version with
# "This is not GNU sed version 4.0" (to satisfy autoconf), so test for it first.
sed_flavor() {
  local v
  v=$(sed --version 2>&1 </dev/null) || true
  case $v in
    *"This is not GNU sed"*) echo busybox ;;
    *"GNU sed"*) echo gnu ;;
    *"illegal option"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# expect LABEL VALUE: fail unless the flavors.sh line for LABEL is exactly "LABEL: VALUE".
expect() {
  expect_line "$1" "$2" exact
}

# expect_prefix LABEL PREFIX: fail unless the flavors.sh line for LABEL starts with "LABEL: PREFIX".
expect_prefix() {
  expect_line "$1" "$2" prefix
}

expect_line() {
  local l
  for l in "${lines[@]}"; do
    case $l in
      "$1: "*)
        case $3 in
          exact) [ "$l" = "$1: $2" ] && return 0 ;;
          prefix) case $l in "$1: $2"*) return 0 ;; *) ;; esac ;;
          *) ;;
        esac
        printf 'got      <%s>\nexpected <%s: %s> (%s)\n' "$l" "$1" "$2" "$3" >&2
        return 1
        ;;
      *) ;;
    esac
  done
  printf 'no line for <%s>\n' "$1" >&2
  return 1
}

run_flavors() {
  run_lab "$LAB/flavors.sh" "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
}

@test "+ ( ) { } and | are literal in a BRE and operators in an ERE, in every grep" {
  run_flavors
  expect 'BRE a+' 'out=a+ st=0 warn=no'
  expect 'ERE a+' 'out=a,aa st=0 warn=no'
  expect 'BRE (ab)' 'out=(ab) st=0 warn=no'
  expect 'ERE (ab)' 'out=ab st=0 warn=no'
  expect 'BRE a{2}' 'out=a{2} st=0 warn=no'
  expect 'ERE a{2}' 'out=aa st=0 warn=no'
  expect 'BRE a|b' 'out=a|b st=0 warn=no'
  expect 'ERE a|b' 'out=a,b st=0 warn=no'
}

@test "ERE anchors are anchors anywhere; a BRE ^ mid-pattern and a leading BRE * are literal" {
  run_flavors
  expect 'BRE a^b' 'out=a^b st=0 warn=no'
  expect 'ERE a^b' 'out=none st=1 warn=no'
  expect 'BRE *a' 'out=*a st=0 warn=no'
}

@test "a leading * in an ERE is undefined: GNU grep warns and matches, BSD and BusyBox grep reject it" {
  run_flavors
  case $(grep_flavor) in
    gnu) expect 'ERE *a' 'out=*a st=0 warn=yes' ;;
    bsd | busybox) expect 'ERE *a' 'out=none st=2 warn=yes' ;;
    *) false ;;
  esac
}

@test "BRE \\+ and \\| are implementation-defined: grep, glibc, and musl make them operators, macOS regcomp keeps them literal" {
  run_flavors
  expect 'BRE a\+' 'out=a,aa st=0 warn=no'
  expect 'BRE a\|b' 'out=a,b st=0 warn=no'
  build_probe
  libc=$("$PROBE" libc)
  probe bre 'a\+' aa
  plus=$output
  probe bre 'x\|b' b
  bar=$output
  case $libc in
    glibc | musl)
      [ "$plus" = 'match 0-2' ]
      [ "$bar" = 'match 0-1' ]
      ;;
    apple)
      [ "$plus" = nomatch ]
      [ "$bar" = nomatch ]
      ;;
    *) false ;;
  esac
}

@test "BRE back-references are POSIX; an ERE \\1 is a back-reference, or a literal 1 in musl and plain macOS regcomp" {
  run_flavors
  expect 'BRE \(ab\)\1' 'out=abab st=0 warn=no'
  build_probe
  libc=$("$PROBE" libc)
  case $(grep_flavor)-$libc in
    gnu-* | bsd-apple | busybox-glibc) expect 'ERE (ab)\1' 'out=abab st=0 warn=no' ;;
    busybox-musl) expect 'ERE (ab)\1' 'out=ab1 st=0 warn=no' ;;
    *) false ;;
  esac
  probe ere '(a)\1' aa
  doubled=$output
  probe ere '(a)\1' a1
  digit=$output
  case $libc in
    glibc)
      [ "$doubled" = 'match 0-2 1=0-1' ]
      [ "$digit" = nomatch ]
      ;;
    musl | apple)
      [ "$doubled" = nomatch ]
      [ "$digit" = 'match 0-2 1=0-1' ]
      ;;
    *) false ;;
  esac
}

@test "\\d is not POSIX: GNU grep matches d, BSD grep and musl match a digit, glibc matches d" {
  run_flavors
  build_probe
  libc=$("$PROBE" libc)
  case $(grep_flavor)-$libc in
    # Upstream GNU grep 3.8+ warns "stray \ before d"; Debian's grep disables that warning
    # (debian/patches/1019724-handle-backslash-warning.patch) unless
    # DEB_GREP_ENABLE_STRAY_BACKSLASH_WARN is set, so warn= is not asserted.
    gnu-*) expect_prefix 'BRE \d' 'out=d st=0 warn=' ;;
    bsd-apple | busybox-musl) expect 'BRE \d' 'out=5 st=0 warn=no' ;;
    busybox-glibc) expect 'BRE \d' 'out=d st=0 warn=no' ;;
    *) false ;;
  esac
  probe bre '\d' 5
  case $libc in
    musl) [ "$output" = 'match 0-1' ] ;;
    glibc | apple) [ "$output" = nomatch ] ;;
    *) false ;;
  esac
}

@test "a backslash in a POSIX bracket is literal; in PCRE [\\n] is a newline, and only GNU grep has -P" {
  run_flavors
  expect 'BRE [\n]' 'out=n,\ st=0 warn=no'
  case $(grep_flavor) in
    gnu)
      expect 'PCRE [\n]' 'out=none st=1 warn=no'
      expect 'PCRE \d' 'out=5 st=0 warn=no'
      ;;
    bsd | busybox)
      expect 'PCRE [\n]' 'out=none st=2 warn=yes'
      expect 'PCRE \d' 'out=none st=2 warn=yes'
      ;;
    *) false ;;
  esac
}

@test "POSIX alternation takes the longest match at the leftmost start; PCRE takes the first alternative that matches" {
  run_flavors
  expect 'sed -E s/a|ab/' 'out=[ab] st=0 warn=no'
  case $(grep_flavor) in
    gnu) expect 'PCRE -o a|ab' 'out=a st=0 warn=no' ;;
    bsd | busybox) expect 'PCRE -o a|ab' 'out=none st=2 warn=yes' ;;
    *) false ;;
  esac
  build_probe
  probe ere 'a|ab' ab
  [ "$output" = 'match 0-2' ]
  probe ere '(wee|week)(knights|night)' weeknights
  [ "$output" = 'match 0-10 1=0-3 2=3-10' ]
}

@test "the POSIX.1-2024 minimal repetition .*? is greedy in glibc and musl and rejected by macOS regcomp" {
  run_flavors
  build_probe
  libc=$("$PROBE" libc)
  case $(sed_flavor) in
    gnu | busybox) expect 'sed -E s/.*?c/' 'out=[abc abc] st=0 warn=no' ;;
    bsd) expect 'sed -E s/.*?c/' 'out=none st=1 warn=yes' ;;
    *) false ;;
  esac
  case $(grep_flavor) in
    gnu)
      expect 'ERE -o .*?c' 'out=abc abc st=0 warn=no'
      expect 'PCRE -o .*?c' 'out=abc, abc st=0 warn=no'
      ;;
    bsd)
      expect 'ERE -o .*?c' 'out=abc, abc st=0 warn=no'
      expect 'PCRE -o .*?c' 'out=none st=2 warn=yes'
      ;;
    busybox)
      expect 'ERE -o .*?c' 'out=abc abc st=0 warn=no'
      expect 'PCRE -o .*?c' 'out=none st=2 warn=yes'
      ;;
    *) false ;;
  esac
  probe ere '.*?c' 'abc abc'
  case $libc in
    glibc | musl) [ "$output" = 'match 0-7' ] ;;
    apple) [ "$output" = 'error REG_BADRPT' ] ;;
    *) false ;;
  esac
}

@test "RE_DUP_MAX is at least the POSIX 255, and a count one past it fails to compile" {
  build_probe
  libc=$("$PROBE" libc)
  probe dupmax
  [ "$status" -eq 0 ]
  dupmax=$output
  [ "$dupmax" -ge 255 ]
  run_lab_c 'getconf RE_DUP_MAX'
  [ "$output" = "$dupmax" ]
  probe bre 'a\{255\}' a
  [ "$output" = nomatch ]
  probe bre "a\\{$((dupmax + 1))\\}" a
  [ "$status" -eq 2 ]
  case $libc in
    glibc)
      [ "$dupmax" -eq 32767 ]
      [ "$output" = 'error REG_ESIZE' ]
      ;;
    musl)
      [ "$dupmax" -eq 255 ]
      [ "$output" = 'error REG_BADBR' ]
      ;;
    apple)
      [ "$dupmax" -eq 255 ]
      [ "$output" = 'error REG_BADMAX' ]
      ;;
    *) false ;;
  esac
}

@test "expr matches a BRE anchored at the first character" {
  run_flavors
  expect 'expr a+b : a+' 'out=2 st=0 warn=no'
  expect 'expr xab : ab' 'out=0 st=1 warn=no'
}

@test "GNU sed --posix turns \\+ back into a literal; BSD and BusyBox sed have no --posix" {
  run_flavors
  case $(sed_flavor) in
    gnu)
      expect 'sed s/a\+/' 'out=[aa] a+ st=0 warn=no'
      # GNU sed 4.10 also warns that \+ is not portable; 4.9 does not, so warn= is not asserted.
      expect_prefix 'sed --posix s/a\+/' 'out=aa [a+] st=0 warn='
      ;;
    busybox)
      expect 'sed s/a\+/' 'out=[aa] a+ st=0 warn=no'
      expect 'sed --posix s/a\+/' 'out=none st=1 warn=yes'
      ;;
    bsd)
      expect 'sed s/a\+/' 'out=aa [a+] st=0 warn=no'
      expect 'sed --posix s/a\+/' 'out=none st=1 warn=yes'
      ;;
    *) false ;;
  esac
}

@test "every awk reads its patterns as EREs and decodes \\n even inside a bracket" {
  local awks awk_cmd flavor
  awks=$(lab_awks)
  [ -n "$awks" ]
  while IFS= read -r awk_cmd; do
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    flavor=$(lab_awk_flavor $awk_cmd)
    # shellcheck disable=SC2086,SC2248 # split a multi-word command such as "busybox awk"
    run_lab "$LAB/awk-ere.sh" $awk_cmd
    [ "$status" -eq 0 ]
    case $flavor in
      gawk | mawk | onetrue | busybox)
        [ "${#lines[@]}" -eq 2 ]
        [ "${lines[0]}" = ere:a ]
        [ "${lines[1]}" = ere:aa ]
        ;;
      *) false ;;
    esac
  done <<EOF
$awks
EOF
}

@test "[[ =~ ]] hands the ERE to the libc in bash, zsh, yash, and BusyBox ash; ksh93 uses its own engine" {
  build_probe
  libc=$("$PROBE" libc)
  run_lab "$LAB/rematch.sh"
  [ "$status" -eq 0 ]
  case $(lab_shell_family)-$libc in
    bash-glibc | zsh-glibc | zsh-sh-glibc | yash-glibc | busybox-glibc | ksh93-*)
      [ "${#lines[@]}" -eq 2 ]
      [ "${lines[0]}" = aa=yes ]
      [ "${lines[1]}" = a1=no ]
      ;;
    bash-musl | zsh-musl | zsh-sh-musl | yash-musl | busybox-musl | bash-apple | zsh-apple | zsh-sh-apple)
      [ "${#lines[@]}" -eq 2 ]
      [ "${lines[0]}" = aa=no ]
      [ "${lines[1]}" = a1=yes ]
      ;;
    mksh-*) [ "$output" = 'no [[ =~ ]]' ] ;;
    dash-* | posh-*)
      # These shells have no [[, so [[ is looked up as a command: on Alpine,
      # /usr/bin/[[ is the BusyBox applet, which evaluates =~ with the libc's regcomp().
      if run_lab_c 'command -v "[["' && [ -n "$output" ]; then
        run_lab "$LAB/rematch.sh"
        [ "$status" -eq 0 ]
        case $libc in
          musl)
            [ "${lines[0]}" = aa=no ]
            [ "${lines[1]}" = a1=yes ]
            ;;
          glibc)
            [ "${lines[0]}" = aa=yes ]
            [ "${lines[1]}" = a1=no ]
            ;;
          *) false ;;
        esac
      else
        run_lab "$LAB/rematch.sh"
        [ "$output" = 'no [[ =~ ]]' ]
      fi
      ;;
    *) false ;;
  esac
}
