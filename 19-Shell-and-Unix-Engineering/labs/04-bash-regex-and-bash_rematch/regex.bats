#!/usr/bin/env bats
# Tests for [[ string =~ ere ]] and BASH_REMATCH: what reaches regcomp, what lands in the array,
# how each shell family differs, and which limits come from the C library rather than the shell.
# shellcheck disable=SC2016 # the single-quoted code is expanded by the shell under test

setup() {
  load ../lib/lab
  SEMVER="$BATS_TEST_DIRNAME/semver.sh"
  REGPROBE="$BATS_TEST_TMPDIR/regprobe"
  "${CC:-cc}" -Wall -Wextra -Werror -o "$REGPROBE" "$BATS_TEST_DIRNAME/regprobe.c"
  # Makes zsh fill BASH_REMATCH from index 0, as bash does; a no-op in every other shell.
  ZSH_AS_BASH='if [ -n "${ZSH_VERSION-}" ]; then setopt BASH_REMATCH KSH_ARRAYS; fi'
  NL='
'
  # What the shell under test runs for [[: a keyword prints "[[", an external command its path.
  lab_shell_words
  DBRACKET=$("${LAB_SHELL_WORDS[@]}" -c 'command -v "[[" || :' 2>/dev/null) || DBRACKET=
}

# external_dbracket: succeed when the shell under test has no [[ keyword but finds a [[ command on
# PATH (Alpine links /usr/bin/[[ to BusyBox, whose test applet implements =~).
external_dbracket() {
  case $DBRACKET in
    /*) return 0 ;;
    *) return 1 ;;
  esac
}

# run_quiet CODE [NAME ARG...]: run_lab_c with CODE's stderr discarded, so the output is only what
# CODE prints. CODE runs as one brace group, so a parse error in it still reaches the output: the
# shell reports it while reading the group, before the redirection takes effect.
run_quiet() {
  local code=$1
  shift
  run_lab_c "{
$code
} 2>/dev/null" "$@"
}

# mksh_rejects: assert that mksh refused the code when it parsed it; mksh has [[ but no =~.
mksh_rejects() {
  [ "$status" -eq 1 ]
  case $output in
    *"unexpected operator/operand '=~'"*) return 0 ;;
    *) return 1 ;;
  esac
}

# libc_status PATTERN STRING: print 0, 1, or 2, the status bash would return if regcomp and regexec
# from this machine's C library were the whole story.
libc_status() {
  local out
  out=$("$REGPROBE" match "$1" "$2") || return 1
  case $out in
    status=0*) echo 0 ;;
    status=1*) echo 1 ;;
    status=2*) echo 2 ;;
    *) return 1 ;;
  esac
}

@test "semver.sh splits valid versions and rejects invalid ones under bash and zsh" {
  run_lab "$SEMVER" 1.22.3 2.0.0-rc.1+build.5 01.2.3 1.2
  case $(lab_shell_family) in
    bash | zsh | zsh-sh)
      [ "$status" -eq 1 ]
      [ "$output" = "1.22.3: major=1 minor=22 patch=3 pre= build=
2.0.0-rc.1+build.5: major=2 minor=0 patch=0 pre=rc.1 build=build.5
01.2.3: invalid
1.2: invalid" ]
      ;;
    yash | busybox | posh | ksh93 | dash | mksh)
      # mksh too: it parses one line at a time, so the guard runs before any [[ =~ ]] is read.
      [ "$status" -eq 2 ]
      [ "$output" = "semver.sh: needs bash or zsh for [[ =~ ]] and BASH_REMATCH" ]
      ;;
    *) false ;;
  esac
}

@test "semver.sh rejects a version with a trailing newline because the ERE dollar anchors at the end of the string" {
  run_lab "$SEMVER" "1.2.3$NL" 1.2.3
  case $(lab_shell_family) in
    bash | zsh | zsh-sh)
      [ "$status" -eq 1 ]
      [ "$output" = "1.2.3
: invalid
1.2.3: major=1 minor=2 patch=3 pre= build=" ]
      ;;
    yash | busybox | posh | ksh93 | dash | mksh)
      [ "$status" -eq 2 ]
      [ "$output" = "semver.sh: needs bash or zsh for [[ =~ ]] and BASH_REMATCH" ]
      ;;
    *) false ;;
  esac
}

@test "each shell family stores the captures somewhere else, or nowhere" {
  run_quiet 're="^([0-9]{4})-([0-9]{2})\$"
    [[ 2026-11 =~ $re ]]
    echo "st=$?"
    (eval '\''echo "bash=${BASH_REMATCH[0]}/${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"'\'') || echo bash=error
    (eval '\''echo "zsh=$MATCH/${match[1]}/${match[2]}"'\'') || echo zsh=error
    (eval '\''echo "ksh=${.sh.match[0]}/${.sh.match[1]}/${.sh.match[2]}"'\'') || echo ksh=error'
  case $(lab_shell_family) in
    bash) [ "$output" = "st=0${NL}bash=2026-11/2026/11${NL}zsh=//${NL}ksh=error" ] ;;
    zsh) [ "$output" = "st=0${NL}bash=//${NL}zsh=2026-11/2026/11${NL}ksh=error" ] ;;
    zsh-sh)
      # KSH_ARRAYS (on in sh emulation) shifts match to index 0, so match[1] is the second group.
      [ "$output" = "st=0${NL}bash=//${NL}zsh=2026-11/11/${NL}ksh=error" ]
      ;;
    ksh93) [ "$output" = "st=0${NL}bash=//${NL}zsh=//${NL}ksh=2026-11/2026/11" ] ;;
    yash) [ "$output" = "st=0${NL}bash=//${NL}zsh=//${NL}ksh=error" ] ;;
    busybox) [ "$output" = "st=0${NL}bash=error${NL}zsh=error${NL}ksh=error" ] ;;
    posh) [ "$output" = "st=127${NL}bash=//${NL}zsh=//${NL}ksh=error" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "st=0${NL}bash=error${NL}zsh=error${NL}ksh=error" ]
      else
        [ "$output" = "st=127${NL}bash=error${NL}zsh=error${NL}ksh=error" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "bash empties BASH_REMATCH on a failed match but keeps the old match when regcomp fails" {
  run_quiet "$ZSH_AS_BASH"'
    re="(b)"; bad="("
    [[ abc =~ $re ]]
    [[ abc =~ $bad ]]
    e=$?
    (eval '\''echo "after-error st=$e n=${#BASH_REMATCH[@]} 0=${BASH_REMATCH[0]}"'\'') || echo "after-error st=$e"
    [[ abc =~ x ]]
    f=$?
    (eval '\''echo "after-fail st=$f n=${#BASH_REMATCH[@]}"'\'') || echo "after-fail st=$f"'
  case $(lab_shell_family) in
    bash) [ "$output" = "after-error st=2 n=2 0=b${NL}after-fail st=1 n=0" ] ;;
    zsh | zsh-sh)
      # zsh keeps the old array on a failed match too, and reports a compile error as 1.
      [ "$output" = "after-error st=1 n=2 0=b${NL}after-fail st=1 n=2" ]
      ;;
    yash | ksh93) [ "$output" = "after-error st=1 n=0 0=${NL}after-fail st=1 n=0" ] ;;
    busybox) [ "$output" = "after-error st=2${NL}after-fail st=1" ] ;;
    posh) [ "$output" = "after-error st=127 n=0 0=${NL}after-fail st=127 n=0" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "after-error st=2${NL}after-fail st=1" ]
      else
        [ "$output" = "after-error st=127${NL}after-fail st=127" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "an uncompilable pattern returns 2 in bash and BusyBox, 1 elsewhere, and [[ ! ]] turns it into success" {
  run_quiet 'bad="("
    [[ abc =~ $bad ]]
    a=$?
    [[ ! abc =~ $bad ]]
    echo "plain=$a negated=$?"'
  case $(lab_shell_family) in
    bash) [ "$output" = "plain=2 negated=0" ] ;;
    zsh | zsh-sh | ksh93 | yash) [ "$output" = "plain=1 negated=0" ] ;;
    busybox) [ "$output" = "plain=2 negated=2" ] ;;
    posh) [ "$output" = "plain=127 negated=127" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "plain=2 negated=2" ]
      else
        [ "$output" = "plain=127 negated=127" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "bash 5.3 reports an uncompilable pattern on stderr; earlier releases fail silently" {
  run_lab_c 'bad="("; [[ abc =~ $bad ]]; echo "st=$?"'
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        5.[3-9]* | [6-9].*)
          [ "${#lines[@]}" -eq 2 ]
          case ${lines[0]} in
            *"invalid regular expression \`('"*) true ;;
            *) false ;;
          esac
          [ "${lines[1]}" = "st=2" ]
          ;;
        *) [ "$output" = "st=2" ] ;;
      esac
      ;;
    zsh | zsh-sh)
      [ "${#lines[@]}" -eq 2 ]
      case ${lines[0]} in
        *"failed to compile regex"*) true ;;
        *) false ;;
      esac
      [ "${lines[1]}" = "st=1" ]
      ;;
    yash | ksh93) [ "$output" = "st=1" ] ;;
    busybox) [ "$output" = "st=2" ] ;;
    posh)
      [ "${#lines[@]}" -eq 2 ]
      [ "${lines[1]}" = "st=127" ]
      ;;
    dash)
      if external_dbracket; then
        [ "$output" = "st=2" ]
      else
        [ "${#lines[@]}" -eq 2 ]
        [ "${lines[1]}" = "st=127" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "quoting any part of the pattern makes it literal in bash and yash, but not in zsh, ksh93, or BusyBox" {
  run_quiet 're="a.b"
    [[ axb =~ "$re" ]]; q=$?
    [[ axb =~ $re ]]; u=$?
    [[ axb =~ a"."b ]]; p=$?
    echo "quoted=$q unquoted=$u part=$p"'
  case $(lab_shell_family) in
    bash | yash) [ "$output" = "quoted=1 unquoted=0 part=1" ] ;;
    zsh | zsh-sh | ksh93 | busybox) [ "$output" = "quoted=0 unquoted=0 part=0" ] ;;
    posh) [ "$output" = "quoted=127 unquoted=127 part=127" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "quoted=0 unquoted=0 part=0" ]
      else
        [ "$output" = "quoted=127 unquoted=127 part=127" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "bash restores the 3.1 rule (quotes do not make the pattern literal) with compat31 and, from 4.3, BASH_COMPAT=31" {
  run_quiet 're="a.b"
    BASH_COMPAT=31
    [[ axb =~ "$re" ]]; a=$?
    unset BASH_COMPAT
    shopt -s compat31
    [[ axb =~ "$re" ]]
    echo "BASH_COMPAT=$a compat31=$?"'
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        3.* | 4.[0-2]*) [ "$output" = "BASH_COMPAT=1 compat31=0" ] ;;
        *) [ "$output" = "BASH_COMPAT=0 compat31=0" ] ;;
      esac
      ;;
    yash) [ "$output" = "BASH_COMPAT=1 compat31=1" ] ;;
    zsh | zsh-sh | ksh93 | busybox) [ "$output" = "BASH_COMPAT=0 compat31=0" ] ;;
    posh) [ "$output" = "BASH_COMPAT=127 compat31=127" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "BASH_COMPAT=0 compat31=0" ]
      else
        [ "$output" = "BASH_COMPAT=127 compat31=127" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "an unquoted backslash in the pattern is shell quoting in bash, while a pattern variable reaches regcomp intact" {
  libc=$(libc_status '^a\sb$' 'a b')
  run_quiet '[[ asb =~ ^a\sb$ ]]; i=$?
    re="^a\\sb\$"
    [[ "a b" =~ $re ]]
    echo "inline=$i var=$?"'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox)
      # The inline backslash quotes "s", so the pattern is ^asb$; the variable passes \s to the C library.
      [ "$output" = "inline=0 var=$libc" ]
      ;;
    ksh93)
      # ksh93 hands the backslash to its own regex engine (libast), where \s is a space class.
      [ "$output" = "inline=1 var=0" ]
      ;;
    posh) [ "$output" = "inline=127 var=127" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "inline=0 var=$libc" ]
      else
        [ "$output" = "inline=127 var=127" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "unquoted parentheses, the blanks inside them, and vertical bars belong to the pattern word in bash" {
  # eval keeps the layout exactly as written; shfmt would otherwise respace it.
  run_lab_c 'eval '\''[[ "a b" =~ ^(a b)$ ]]'\''; echo "parens=$?"'
  parens_status=$status
  parens_output=$output
  run_lab_c 'eval '\''[[ ab =~ ^a|x ]]'\''; echo "bar=$?"'
  case $(lab_shell_family) in
    bash | yash | ksh93)
      [ "$parens_output" = "parens=0" ]
      [ "$output" = "bar=0" ]
      ;;
    zsh)
      [ "$parens_output" = "parens=0" ]
      case $output in
        *"parse error near \`|'"*) true ;;
        *) false ;;
      esac
      ;;
    zsh-sh)
      # SH_GLOB, on in sh emulation, stops zsh from reading parentheses as part of a word.
      case $parens_output in
        *"parse error near \`('"*) true ;;
        *) false ;;
      esac
      case $output in
        *"parse error near \`|'"*) true ;;
        *) false ;;
      esac
      ;;
    busybox | dash | posh)
      # No [[ grammar: ( is a syntax error, and | starts a pipeline into a command named x.
      [ "$parens_status" -ne 0 ]
      case $parens_output in
        *"("*) true ;;
        *) false ;;
      esac
      case $output in
        *"x: not found"*) true ;;
        *) false ;;
      esac
      ;;
    mksh)
      [ "$parens_status" -ne 0 ]
      case $parens_output in
        *"=~"*) true ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "a subexpression that took no part in the match is an empty but set element of BASH_REMATCH" {
  run "$REGPROBE" match '^(a)?(b)$' b
  [ "$status" -eq 0 ]
  [ "$output" = "status=0${NL}n=3${NL}0=[b]${NL}1=<unset>${NL}2=[b]" ]
  run_quiet "$ZSH_AS_BASH"'
    re="^(a)?(b)\$"
    [[ b =~ $re ]]
    (eval '\''echo "n=${#BASH_REMATCH[@]} 1=[${BASH_REMATCH[1]-unset}] 2=[${BASH_REMATCH[2]}]"'\'') || echo error'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh) [ "$output" = "n=3 1=[] 2=[b]" ] ;;
    yash | ksh93 | posh) [ "$output" = "n=0 1=[unset] 2=[]" ] ;;
    busybox | dash) [ "$output" = "error" ] ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "POSIX leftmost-longest matching: alternation takes the longest branch and a repeated group keeps its last iteration" {
  run "$REGPROBE" match 'x|xy|xyz' xyz
  [ "$output" = "status=0${NL}n=1${NL}0=[xyz]" ]
  run "$REGPROBE" match '(wee|week)(knights|night)' weeknights
  [ "$output" = "status=0${NL}n=3${NL}0=[weeknights]${NL}1=[wee]${NL}2=[knights]" ]
  run "$REGPROBE" match '^(a|b)+$' aab
  [ "$output" = "status=0${NL}n=2${NL}0=[aab]${NL}1=[b]" ]
  run_quiet "$ZSH_AS_BASH"'
    r1="x|xy|xyz"; r2="(wee|week)(knights|night)"; r3="^(a|b)+\$"
    [[ xyz =~ $r1 ]]
    (eval '\''printf "%s " "${BASH_REMATCH[0]}"'\'')
    [[ weeknights =~ $r2 ]]
    (eval '\''printf "%s/%s " "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"'\'')
    [[ aab =~ $r3 ]]
    (eval '\''printf "%s\n" "${BASH_REMATCH[1]}"'\'') || echo error'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh) [ "$output" = "xyz wee/knights b" ] ;;
    yash | ksh93 | posh) [ "$output" = " / " ] ;;
    busybox | dash) [ "$output" = "error" ] ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "without REG_NEWLINE the anchors bind to the whole value and the period matches a newline, unlike line-oriented grep" {
  run_quiet 'nl="
"
    s="12${nl}x"
    re="^[0-9]+\$"; dot="^12.x\$"
    [[ $s =~ $re ]]; a=$?
    [[ $s =~ $dot ]]; d=$?
    [[ "12$nl" =~ $re ]]; t=$?
    printf "12\nx\n" | grep -Eq "$re"
    echo "anchored=$a dot=$d trailing=$t grep=$?"'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | ksh93)
      [ "$output" = "anchored=1 dot=0 trailing=1 grep=0" ]
      ;;
    busybox)
      # BusyBox field-splits the unquoted $s at the newline, so [[ sees "12 x =~ ..." and fails with 2.
      [ "$output" = "anchored=2 dot=2 trailing=1 grep=0" ]
      ;;
    posh) [ "$output" = "anchored=127 dot=127 trailing=127 grep=0" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "anchored=2 dot=2 trailing=1 grep=0" ]
      else
        [ "$output" = "anchored=127 dot=127 trailing=127 grep=0" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "BusyBox field-splits an unquoted left operand of =~, so it must be quoted there" {
  run_quiet 'v="a b"
    [[ $v =~ ^a ]]; u=$?
    [[ "$v" =~ ^a ]]
    echo "unquoted=$u quoted=$?"'
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | ksh93) [ "$output" = "unquoted=0 quoted=0" ] ;;
    busybox) [ "$output" = "unquoted=2 quoted=0" ] ;;
    posh) [ "$output" = "unquoted=127 quoted=127" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "unquoted=2 quoted=0" ]
      else
        [ "$output" = "unquoted=127 quoted=127" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "nocasematch makes =~ case-insensitive in bash; nocaseglob did too before bash 5.1" {
  run_quiet 're="^abc\$"
    shopt -s nocaseglob; [[ ABC =~ $re ]]; g=$?; shopt -u nocaseglob
    shopt -s nocasematch; [[ ABC =~ $re ]]
    echo "nocaseglob=$g nocasematch=$?"'
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        [34].* | 5.0*) [ "$output" = "nocaseglob=0 nocasematch=0" ] ;;
        *) [ "$output" = "nocaseglob=1 nocasematch=0" ] ;;
      esac
      ;;
    zsh | zsh-sh | yash | ksh93 | busybox) [ "$output" = "nocaseglob=1 nocasematch=1" ] ;;
    posh) [ "$output" = "nocaseglob=127 nocasematch=127" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "nocaseglob=1 nocasematch=1" ]
      else
        [ "$output" = "nocaseglob=127 nocasematch=127" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "BASH_REMATCH was readonly before bash 5.1 and a local copy shields the caller only from bash 5.3" {
  run_quiet 're="(b)"
    f() { local BASH_REMATCH; r="z+"; [[ zz =~ $r ]]; }
    [[ abc =~ $re ]]
    f
    (eval '\''echo "after=${BASH_REMATCH[0]}"'\'') || echo after=error
    (eval '\''BASH_REMATCH=(x)'\'')
    echo "assign=$?"'
  case $(lab_shell_family) in
    bash)
      case $(lab_shell_version) in
        [34].* | 5.0*)
          # Both the local and the assignment fail as readonly; POSIX-mode bash (macOS /bin/sh)
          # exits the subshell with 127 instead of 1.
          case $output in
            "after=zz${NL}assign=1" | "after=zz${NL}assign=127") true ;;
            *) false ;;
          esac
          ;;
        5.[12]*) [ "$output" = "after=zz${NL}assign=0" ] ;;
        *) [ "$output" = "after=b${NL}assign=0" ] ;;
      esac
      ;;
    zsh | zsh-sh | yash | ksh93)
      # None of them writes BASH_REMATCH unless asked (zsh: setopt BASH_REMATCH).
      [ "$output" = "after=${NL}assign=0" ]
      ;;
    busybox | dash) [ "$output" = "after=error${NL}assign=2" ] ;;
    posh) [ "$output" = "after=${NL}assign=1" ] ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "the interval limit is the C library's RE_DUP_MAX, and bash rejects one more with status 2" {
  run "$REGPROBE" limits
  [ "$status" -eq 0 ]
  max=${output#RE_DUP_MAX=}
  conf=$(getconf RE_DUP_MAX)
  [ "$conf" = "$max" ]
  over=$((max + 1))
  at_max=$(libc_status "a{$max}" a)
  [ "$at_max" = 1 ]
  past_max=$(libc_status "a{$over}" a)
  [ "$past_max" = 2 ]
  run_quiet 'over=$1; re="a{$over}"; [[ a =~ $re ]]; echo "st=$?"' sh "$over"
  case $(lab_shell_family) in
    bash | busybox) [ "$output" = "st=2" ] ;;
    zsh | zsh-sh | yash | ksh93) [ "$output" = "st=1" ] ;;
    posh) [ "$output" = "st=127" ] ;;
    dash)
      if external_dbracket; then
        [ "$output" = "st=2" ]
      else
        [ "$output" = "st=127" ]
      fi
      ;;
    mksh) mksh_rejects ;;
    *) false ;;
  esac
}

@test "the C library matches by character in a UTF-8 locale and by byte in the C locale" {
  e=$(printf '\303\251')
  LC_ALL=C.UTF-8 run "$REGPROBE" match '^.$' "$e"
  [ "$output" = "status=0${NL}n=1${NL}0=[$e]" ]
  LC_ALL=C run "$REGPROBE" match '^.$' "$e"
  [ "$output" = "status=1" ]
  LC_ALL=C run "$REGPROBE" match '^..$' "$e"
  [ "$output" = "status=0${NL}n=1${NL}0=[$e]" ]
}

@test "no C library in the matrix implements the POSIX.1-2024 shortest-match modifier" {
  # XBD 9.4.6 makes a+? match "a"; glibc and musl read it as (a+)? and match "aaa", macOS rejects it.
  run "$REGPROBE" match 'a+?' aaa
  [ "$status" -eq 0 ]
  case $output in
    "status=2" | "status=0${NL}n=1${NL}0=[aaa]") true ;;
    *) false ;;
  esac
}

@test "the ERE extensions bash inherits differ by C library: GNU escapes, TRE escapes, or neither" {
  run "$REGPROBE" libc
  [ "$status" -eq 0 ]
  libc=${output#libc=}
  got=
  for pair in '\w|a' '\d|1' '\s| ' '(a)\1|aa' 'a{,2}|a' '[[:<:]]a|a' '|a'; do
    st=$(libc_status "${pair%%|*}" "${pair#*|}")
    got="$got$st"
  done
  # Order: \w \d \s back-reference {,n} [[:<:]] empty-pattern; 0 match, 1 no match, 2 rejected.
  case $libc in
    glibc) [ "$got" = 0100020 ] ;; # GNU \w \s and ERE back-references; no \d
    apple) [ "$got" = 1111102 ] ;; # escapes are literals; only [[:<:]] word bounds; empty pattern rejected
    other) [ "$got" = 0001220 ] ;; # musl (TRE): \w \d \s; back-references only in BREs; {,n} rejected
    *) false ;;
  esac
}
