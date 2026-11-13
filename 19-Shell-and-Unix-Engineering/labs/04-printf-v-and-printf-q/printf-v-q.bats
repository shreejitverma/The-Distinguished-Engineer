#!/usr/bin/env bats
# Tests for printf -v and printf %q: which shells have them, what they store and print,
# how bash versions differ, and the injection and truncation traps.
# Code runs only through run_lab or run_lab_c, so LAB_SHELL picks the shell under test.

setup() {
  load ../lib/lab
  LAB_DIR="$BATS_TEST_DIRNAME"
  FAMILY=$(lab_shell_family)
  VERSION=$(lab_shell_version)
}

# bash_at_least MAJOR MINOR: succeed when the bash under test is release MAJOR.MINOR or later.
bash_at_least() {
  local major minor rest
  major=${VERSION%%.*}
  rest=${VERSION#*.}
  minor=${rest%%.*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# is_ksh93um: succeed when the ksh93 under test is ksh93u+m (printf -v since 2021-11-18), not ksh93u+ 2012.
is_ksh93um() {
  case $VERSION in
    *93u+m*) return 0 ;;
    *) return 1 ;;
  esac
}

# printf_impl: name the external printf utility on PATH (gnu, uutils, busybox, bsd, or unknown).
# mksh and posh run it, because neither ships a printf builtin that takes precedence over it.
printf_impl() {
  local path real out
  path=$(type -P printf) || {
    echo unknown
    return
  }
  real=$(readlink -f "$path" 2>/dev/null) || real=$path
  case ${real##*/} in
    busybox*)
      echo busybox
      return
      ;;
    *) ;;
  esac
  out=$("$path" --version 2>&1 </dev/null) || true
  case $out in
    *"GNU coreutils"*) echo gnu ;;
    *uutils*) echo uutils ;;
    *BusyBox*) echo busybox ;;
    *"usage: printf format"*) echo bsd ;;
    *) echo unknown ;;
  esac
}

# Shell code that runs BODY only where printf -v really assigns, detected by effect, else prints no-v.
has_v() {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  printf 'if (p=; printf -v p %%s ok >/dev/null 2>&1 && [ "$p" = ok ]) 2>/dev/null; then %s; else echo no-v; fi' "$1"
}

@test "printf -v assigns only where the shell implements it; BusyBox and GNU printf treat -v as the format" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'x=unset; printf -v x "%s-%s" a b 2>/dev/null; echo "[$x]"'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh) [ "$output" = "[a-b]" ] ;;
    ksh93) if is_ksh93um; then [ "$output" = "[a-b]" ]; else [ "$output" = "[unset]" ]; fi ;;
    dash | yash) [ "$output" = "[unset]" ] ;;
    busybox) [ "$output" = "-v[unset]" ] ;;
    mksh | posh)
      case $(printf_impl) in
        gnu | busybox) [ "$output" = "-v[unset]" ] ;;
        bsd) [ "$output" = "[unset]" ] ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "printf -v keeps the trailing newlines that command substitution strips" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  code=$(has_v 'printf -v x "a\n\n"; y=$(printf "a\n\n"); echo "${#x} ${#y}"')
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh) [ "$output" = "3 1" ] ;;
    ksh93) if is_ksh93um; then [ "$output" = "3 1" ]; else [ "$output" = no-v ]; fi ;;
    dash | yash | busybox | mksh | posh) [ "$output" = no-v ] ;;
    *) false ;;
  esac
}

@test "printf -v ends a bash or ksh93 value at a NUL byte, while zsh keeps all three bytes" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  code=$(has_v 'printf -v x "a\0b"; echo "${#x}"')
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = 1 ] ;;
    zsh | zsh-sh) [ "$output" = 3 ] ;;
    ksh93) if is_ksh93um; then [ "$output" = 1 ]; else [ "$output" = no-v ]; fi ;;
    dash | yash | busybox | mksh | posh) [ "$output" = no-v ] ;;
    *) false ;;
  esac
}

@test "printf -v never touches standard output, so it succeeds with standard output closed" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  code=$(has_v 'printf -v x hi >&-; echo "st=$? x=$x"')
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh) [ "$output" = "st=0 x=hi" ] ;;
    ksh93) if is_ksh93um; then [ "$output" = "st=0 x=hi" ]; else [ "$output" = no-v ]; fi ;;
    dash | yash | busybox | mksh | posh) [ "$output" = no-v ] ;;
    *) false ;;
  esac
}

@test "an empty format leaves the variable unchanged before bash 4.4 and empties it from 4.4 and in zsh" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'if [ -n "${BASH_VERSION-}${ZSH_VERSION-}" ]; then x=old; printf -v x ""; echo "[$x]"; else echo n/a; fi'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) if bash_at_least 4 4; then [ "$output" = "[]" ]; else [ "$output" = "[old]" ]; fi ;;
    zsh | zsh-sh) [ "$output" = "[]" ] ;;
    dash | yash | busybox | mksh | posh | ksh93) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "a printf -v into a readonly variable returns 0 before bash 5.0, 1 from 5.0, and aborts zsh with status 0" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'if [ -n "${BASH_VERSION-}${ZSH_VERSION-}" ]; then
  (readonly r=1; printf -v r 2; echo "st=$? r=$r") 2>/dev/null; echo "sub=$?"
else echo n/a; fi'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) if bash_at_least 5 0; then [ "$output" = "$(printf 'st=1 r=1\nsub=0')" ]; else [ "$output" = "$(printf 'st=0 r=1\nsub=0')" ]; fi ;;
    zsh | zsh-sh) [ "$output" = "sub=0" ] ;;
    dash | yash | busybox | mksh | posh | ksh93) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "bash 4.1+ and zsh run a command substitution hidden in a printf -v array subscript" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'if [ -n "${BASH_VERSION-}${ZSH_VERSION-}" ]; then
  cd "$1" || exit 1
  printf -v "b[$2]" x 2>/dev/null
  st=$?
  eval "bv=\${b[1]-}"
  echo "st=$st ran=[$(cat pw 2>/dev/null)] b=$bv"
else echo n/a; fi' sh "$BATS_TEST_TMPDIR" '$(echo ran >pw)1'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) if bash_at_least 4 1; then [ "$output" = "st=0 ran=[ran] b=x" ]; else [ "$output" = "st=2 ran=[] b=" ]; fi ;;
    zsh | zsh-sh) [ "$output" = "st=0 ran=[ran] b=x" ] ;;
    dash | yash | busybox | mksh | posh | ksh93) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "bash 5.3 shopt array_expand_once stops the printf -v subscript from being expanded" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  # The unexpanded subscript is an arithmetic syntax error, which ends a bash -c shell, so it runs in a subshell.
  run_lab_c '[ -n "${BASH_VERSION-}" ] || { echo n/a; exit 0; }
cd "$1" || exit 1
if shopt -s array_expand_once 2>/dev/null; then (printf -v "b[$2]" x) 2>/dev/null; echo "sub=$?"; else echo noopt; fi
echo "ran=[$(cat pw 2>/dev/null)]"' sh "$BATS_TEST_TMPDIR" '$(echo ran >pw)1'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) if bash_at_least 5 3; then [ "$output" = "$(printf 'sub=1\nran=[]')" ]; else [ "$output" = "$(printf 'noopt\nran=[]')" ]; fi ;;
    zsh | zsh-sh | dash | yash | busybox | mksh | posh | ksh93) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "zsh and ksh93u+ evaluate a printf %d argument as arithmetic, running a subscript's command substitution" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'case ${BASH_VERSION-}${ZSH_VERSION-}${KSH_VERSION-} in
  [0-9]* | *93*)
    cd "$1" || exit 1
    eval "w=(5 6)"
    printf "%d\n" "$2" 2>/dev/null; echo "ran=[$(cat pz 2>/dev/null)]" ;;
  *) echo n/a ;;
esac' sh "$BATS_TEST_TMPDIR" 'w[$(echo ran >pz)1]'
  case $FAMILY in
    bash) [ "$output" = "$(printf '0\nran=[]')" ] ;;
    zsh) [ "$output" = "$(printf '5\nran=[ran]')" ] ;;
    zsh-sh) [ "$output" = "$(printf '6\nran=[ran]')" ] ;; # sh emulation sets KSH_ARRAYS, so w[1] is the second element
    ksh93) if is_ksh93um; then [[ $output == *"ran=[]" ]]; else [ "$output" = "$(printf '6\nran=[ran]')" ]; fi ;;
    dash | yash | busybox | mksh | posh) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "bash and zsh fill a printf -v array differently when the format is reused" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'if [ -n "${BASH_VERSION-}${ZSH_VERSION-}" ]; then
  eval "a=(); printf -v a %s x y z; echo \"\${#a[@]} \${a[*]}\""
else echo n/a; fi'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "1 xyz" ] ;;
    zsh | zsh-sh) [ "$output" = "3 x y z" ] ;;
    dash | yash | busybox | mksh | posh | ksh93) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "printf %q output has a different dialect in every shell, and dash, BusyBox, and BSD printf reject it" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'printf "%q|" "a b" "it'"'"'s" "" "\$(id)" "~x" 2>/dev/null; echo " st=$?"'
  case $FAMILY in
    bash)
      if bash_at_least 4 3; then
        [ "$output" = "a\\ b|it\\'s|''|\\\$\\(id\\)|\\~x| st=0" ]
      else
        [ "$output" = "a\\ b|it\\'s|''|\\\$\\(id\\)|~x| st=0" ]
      fi
      ;;
    zsh | zsh-sh) [ "$output" = "a\\ b|it\\'s|''|\\\$\\(id\\)|\\~x| st=0" ] ;;
    ksh93) [ "$output" = "'a b'|\$'it\\'s'|''|'\$(id)'|'~x'| st=0" ] ;;
    dash) [ "$output" = " st=2" ] ;;
    busybox) [ "$output" = " st=1" ] ;;
    yash) [[ $output == " st="[12] ]] ;; # yash has no %q; the exact failure status is not documented
    mksh | posh)
      case $(printf_impl) in
        gnu) [ "$output" = "'a b'|\"it's\"|''|'\$(id)'|'~x'| st=0" ] ;;
        busybox | bsd) [ "$output" = " st=1" ] ;;
        *) false ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "printf %q with no argument quotes an empty string in bash only" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'case ${BASH_VERSION-}${ZSH_VERSION-}${KSH_VERSION-} in
  [0-9]* | *93*) printf "[%q]\n" ;;
  *) echo n/a ;;
esac'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "['']" ] ;;
    zsh | zsh-sh | ksh93) [ "$output" = "[]" ] ;;
    dash | yash | busybox | mksh | posh) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "a %q precision cuts the quoted text mid-escape; bash 5.2 %Q cuts the argument first" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'if [ -n "${BASH_VERSION-}" ]; then printf "[%.2q]" "a b"; printf "[%.3Q]" "a b c" 2>/dev/null; echo; else echo n/a; fi'
  case $FAMILY in
    bash) if bash_at_least 5 2; then [ "$output" = '[a\][a\ b]' ]; else [ "$output" = '[a\][' ]; fi ;;
    zsh | zsh-sh | dash | yash | busybox | mksh | posh | ksh93) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "bash 5.3 %#q forces single quotes" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'if [ -n "${BASH_VERSION-}" ]; then printf "%#q\n" "a b"; else echo n/a; fi'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) if bash_at_least 5 3; then [ "$output" = "'a b'" ]; else [ "$output" = 'a\ b' ]; fi ;;
    zsh | zsh-sh | dash | yash | busybox | mksh | posh | ksh93) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "bash %q keeps a printable UTF-8 character raw and writes an invalid byte as octal from 4.3" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'if [ -n "${BASH_VERSION-}" ]; then
  LC_ALL=C.UTF-8
  printf "%q %q\n" "$(printf "\303\251")" "$(printf "\377")"
else echo n/a; fi'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 4 3; then
        [ "$output" = "$(printf '\303\251')"" \$'\\377'" ]
      else
        [ "$output" = "$(printf '\303\251 \377')" ]
      fi
      ;;
    zsh | zsh-sh | dash | yash | busybox | mksh | posh | ksh93) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "bash %q doubles a run of spaces and turns control bytes into 4n + 3 bytes" {
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'if [ -n "${BASH_VERSION-}" ]; then
  s=$(head -c 1000 /dev/zero | tr "\000" " "); printf -v q %q "$s"; eval "back=$q"
  c=$(head -c 1000 /dev/zero | tr "\000" "\001"); printf -v r %q "$c"; eval "cback=$r"
  echo "${#s} ${#q} ${#r} $([ "$back" = "$s" ] && [ "$cback" = "$c" ] && echo same)"
else echo n/a; fi'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash) [ "$output" = "1000 2000 4003 same" ] ;;
    zsh | zsh-sh | dash | yash | busybox | mksh | posh | ksh93) [ "$output" = n/a ] ;;
    *) false ;;
  esac
}

@test "shquote.sh prints each shell's own quoting, and the line parses back into the same arguments" {
  nl=$(printf 'c\nd')
  # shellcheck disable=SC2016 # the command substitution text is data for the shell under test
  run_lab "$LAB_DIR/shquote.sh" 'a b' "it's" '' '$(echo pwned)' '*' "$nl"
  [ "$status" -eq 0 ]
  line=$output
  case $FAMILY in
    bash) [ "$line" = "a\\ b it\\'s '' \\\$\\(echo\\ pwned\\) \\* \$'c\\nd'" ] ;;
    zsh | zsh-sh) [ "$line" = "a\\ b it\\'s '' \\\$\\(echo\\ pwned\\) \\* c\$'\\n'd" ] ;;
    ksh93)
      if is_ksh93um; then
        [ "$line" = "'a b' \$'it\\'s' '' '\$(echo pwned)' '*' \$'c\\nd'" ]
      else
        [ "$line" = "'a b' 'it'\\''s' '' '\$(echo pwned)' '*' '$nl'" ]
      fi
      ;;
    dash | yash | busybox | mksh | posh) [ "$line" = "'a b' 'it'\\''s' '' '\$(echo pwned)' '*' '$nl'" ] ;;
    *) false ;;
  esac
  # shellcheck disable=SC2016 # the expansions belong to the shell under test
  run_lab_c 'eval "set -- $1"; echo "$#"; printf "<%s>" "$@"' sh "$line"
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # the command substitution text is data, never run
  [ "$output" = "$(printf '6\n<a b><it'"'"'s><><$(echo pwned)><*><c\nd>')" ]
}

@test "shquote.sh with no arguments prints an empty line, not a quoted empty word" {
  run_lab "$LAB_DIR/shquote.sh"
  [ "$status" -eq 0 ]
  [ "$output" = "" ]
}
