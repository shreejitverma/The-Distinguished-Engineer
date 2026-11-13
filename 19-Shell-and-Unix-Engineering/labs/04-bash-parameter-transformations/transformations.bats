#!/usr/bin/env bats
# Bash parameter transformations (${name@op}) and case modification (${name^^}): what each
# operator produces, which bash release added it, the bugs between releases, and what every other
# shell in the matrix does with the same text.
# shellcheck disable=SC2016 # every code string is expanded by the shell under test, not by bats

setup() {
  load ../lib/lab
  LAB_DIR=$BATS_TEST_DIRNAME
  FAMILY=$(lab_shell_family)
  BV=$(bash_ver)
}

# bash_ver: print the bash release under test as major * 10 + minor (53 for 5.3), or 0 for another family.
bash_ver() {
  local v major minor family
  family=$(lab_shell_family)
  if [ "$family" != bash ]; then
    echo 0
    return
  fi
  v=$(lab_shell_version)
  major=${v%%.*}
  v=${v#*.}
  minor=${v%%.*}
  echo $((major * 10 + minor))
}

# mksh_2020: succeed when the shell under test is the mksh R59 2020/10/31 build (Alpine), which quotes differently.
mksh_2020() {
  case $(lab_shell_version) in
    *"R59 2020/"*) return 0 ;;
    *) return 1 ;;
  esac
}

# dash_has_dollar_single: succeed when the shell understands $'...' (dash 0.5.13 and later; 0.5.12 does not).
# It runs the shell directly, not through run, so it leaves $output and $status alone.
dash_has_dollar_single() {
  local got
  lab_shell_words
  got=$("${LAB_SHELL_WORDS[@]}" -c 'printf %s $'"'"'a'"'" 2>/dev/null) || true
  [ "$got" = a ]
}

@test "@Q exists only in bash 4.4+ and mksh" {
  run_lab_c 'x=$(printf "it\047s a b"); printf "%s\n" "${x@Q}"'
  expected="'it'\\''s a b'"
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "$expected" ]
      else
        [ "$status" -ne 0 ]
        [[ $output == *"bad substitution"* ]]
      fi
      ;;
    mksh)
      [ "$status" -eq 0 ]
      [ "$output" = "$expected" ]
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93)
      [ "$status" -ne 0 ]
      [ "$output" != "$expected" ]
      ;;
    *) false ;;
  esac
}

@test "operator letters accepted by each release: 4.4 QEPAa, 5.1 adds KUuL, 5.2 adds k" {
  got=
  for op in Q E P A a K U u L k; do
    run_lab_c "x=v; : \"\${x@$op}\" && printf ok"
    if [ "$output" = ok ]; then
      got=$got$op
    fi
  done
  case $FAMILY in
    bash)
      if [ "$BV" -ge 52 ]; then
        [ "$got" = QEPAaKUuLk ]
      elif [ "$BV" -ge 51 ]; then
        [ "$got" = QEPAaKUuL ]
      elif [ "$BV" -ge 44 ]; then
        [ "$got" = QEPAa ]
      else
        [ -z "$got" ]
      fi
      ;;
    mksh) [ "$got" = Q ] ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93) [ -z "$got" ] ;;
    *) false ;;
  esac
}

@test "bash 3.2 silently ignores an operator after an array subscript" {
  run_lab_c 'a=(x "y z"); printf "<%s>" "${a[@]@Q}" "${a[0]junk}"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -ne 0 ]
        [[ $output == *'${a[0]junk}: bad substitution'* ]]
      elif [ "$BV" -le 32 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "<x><y z><x>" ]
      else
        false
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@Q of a quoted array subscript quotes each element (bash 4.4+)" {
  run_lab_c 'a=(x "y z" ""); printf "<%s>" "${a[@]@Q}"; printf "\n"; printf "<%s>" "${a[*]@Q}"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "${lines[0]}" = "<'x'><'y z'><''>" ]
        [ "${lines[1]}" = "<'x' 'y z' ''>" ]
      else
        [ "$output" = "$(printf '<x><y z><>\n<x y z >')" ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "an unknown operator is fatal from bash 5.2, a recoverable error before; zsh and ksh93 recover" {
  run_lab "$LAB_DIR/badop.sh"
  case $FAMILY in
    bash)
      [[ $output == *'${x@Z}: bad substitution'* ]]
      if [ "$BV" -ge 52 ]; then
        [ "$status" -eq 1 ]
        [[ $output != *after* ]]
      else
        [ "$status" -eq 0 ]
        [ "${lines[1]}" = "after 1" ]
      fi
      ;;
    zsh | zsh-sh | ksh93)
      [[ $output == *"bad substitution"* ]]
      [ "$status" -eq 0 ]
      [ "${lines[1]}" = "after 1" ]
      ;;
    dash | posh | busybox | yash | mksh)
      [ "$status" -ne 0 ]
      [[ $output != *after* ]]
      ;;
    *) false ;;
  esac
}

@test "an unknown operator on an unset variable expands to nothing in bash 4.4+" {
  run_lab_c 'unset u; printf "[%s]\n" "${u@Z}"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "[]" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "set -u stops @Q of an unset variable from bash 5.1; 4.4 and mksh expand it" {
  run_lab_c '(set -u; unset u; printf "[%s]\n" "${u@Q}"; echo after); echo "status=$?"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 51 ]; then
        [[ $output == *"u: unbound variable"* ]]
        [[ $output != *after* ]]
        [ "${lines[1]}" = status=1 ]
      elif [ "$BV" -ge 44 ]; then
        [ "$output" = "$(printf '[]\nafter\nstatus=0')" ]
      else
        [[ $output == *"bad substitution"* ]]
        [ "${lines[1]}" = status=1 ]
      fi
      ;;
    mksh) [ "$output" = "$(printf "['']\nafter\nstatus=0")" ] ;;
    zsh | zsh-sh | dash | posh | busybox | ksh93)
      [[ $output != *after* ]]
      [[ ${lines[1]} == status=[1-9]* ]]
      ;;
    yash)
      # yash rejects the operator while parsing, so not even the status line runs.
      [ "$status" -ne 0 ]
      [[ $output == *"invalid character \`@' in parameter expansion"* ]]
      ;;
    *) false ;;
  esac
}

@test "@Q survives eval for quotes, newlines, control bytes and command text" {
  run_lab_c 'x=$(printf "a\047b\n\001\044(id)\140*\tz."); printf "%s\n" "${x@Q}"; eval "y=${x@Q}"; [ "$x" = "$y" ] && echo same'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "${lines[0]}" = "\$'a\\'b\\n\\001\$(id)\`*\\tz.'" ]
        [ "${lines[1]}" = same ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    mksh)
      [ "$status" -eq 0 ]
      [ "${lines[1]}" = same ]
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@Q output size: 4n+2 bytes for n single quotes, 4n+3 for n control bytes" {
  run_lab_c 'q=$(printf "%1000s" "" | tr " " "\047"); c=$(printf "%1000s" "" | tr " " "\001"); v=${q@Q}; w=${c@Q}; printf "%s %s %s %s\n" "${#q}" "${#v}" "${#c}" "${#w}"; eval "q2=$v; c2=$w"; [ "$q" = "$q2" ] && [ "$c" = "$c2" ] && echo same'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "${lines[0]}" = "1000 4002 1000 4003" ]
        [ "${lines[1]}" = same ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    mksh)
      # mksh writes a single quote as \' outside any quotes: 2 bytes each instead of 4.
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "1000 2000 1000 4003" ]
      [ "${lines[1]}" = same ]
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "an unquoted @Q result is field split like any other expansion" {
  run_lab_c 'x="a b"; printf "<%s>" ${x@Q}'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$output" = "<'a><b'>" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    mksh) [ "$output" = "<'a><b'>" ] ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@Q of a tilde: bash and mksh 2025 quote it, mksh R59 2020 leaves it bare" {
  run_lab_c 'x="~"; printf "%s\n" "${x@Q}"; eval "y=${x@Q}"; printf "%s\n" "$y"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$output" = "$(printf "'~'\n~")" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    mksh)
      if mksh_2020; then
        [ "${lines[0]}" = "~" ]
        [ "${lines[1]}" = "$HOME" ]
      else
        [ "$output" = "$(printf "'~'\n~")" ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "bash 4.4+ and mksh write the four fixture strings byte for byte" {
  run_lab_c 'n=$(printf "a\nb"); e=$(printf "\033[0m"); c=$(printf "\001x"); s=$(printf "it\047s"); printf "y=%s\n" "${c@Q}" "${e@Q}" "${n@Q}" "${s@Q}"'
  expected=$(cat "$LAB_DIR/fixtures/ctl.q" "$LAB_DIR/fixtures/esc.q" "$LAB_DIR/fixtures/newline.q" "$LAB_DIR/fixtures/squote.q")
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "$expected" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    mksh)
      [ "$status" -eq 0 ]
      [ "$output" = "$expected" ]
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "every shell reads the plain single-quote form of @Q" {
  run_lab "$LAB_DIR/consume.sh" "$LAB_DIR/fixtures/squote.q"
  [ "$status" -eq 0 ]
  [ "$output" = 69742773 ]
}

@test "the \$'a\\nb' form needs \$'...' support: dash 0.5.12 and posh read it literally" {
  run_lab "$LAB_DIR/consume.sh" "$LAB_DIR/fixtures/newline.q"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | mksh | yash | busybox | ksh93) [ "$output" = 610a62 ] ;;
    posh) [ "$output" = 24615c6e62 ] ;;
    dash)
      if dash_has_dollar_single; then
        [ "$output" = 610a62 ]
      else
        [ "$output" = 24615c6e62 ]
      fi
      ;;
    *) false ;;
  esac
}

@test "the \\E that @Q writes for ESC is not POSIX: BusyBox, yash and dash misread it" {
  run_lab "$LAB_DIR/consume.sh" "$LAB_DIR/fixtures/esc.q"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | mksh | ksh93) [ "$output" = 1b5b306d ] ;;
    busybox) [ "$output" = 5c455b306d ] ;;
    yash) [ "$output" = 3f455b306d ] ;;
    posh) [ "$output" = 245c455b306d ] ;;
    dash)
      if dash_has_dollar_single; then
        [ "$output" = 5c455b306d ]
      else
        [ "$output" = 245c455b306d ]
      fi
      ;;
    *) false ;;
  esac
}

@test "octal escapes in \$'...' decode wherever \$'...' exists" {
  run_lab "$LAB_DIR/consume.sh" "$LAB_DIR/fixtures/ctl.q"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | zsh | zsh-sh | mksh | yash | busybox | ksh93) [ "$output" = 0178 ] ;;
    posh) [ "$output" = 245c30303178 ] ;;
    dash)
      if dash_has_dollar_single; then
        [ "$output" = 0178 ]
      else
        [ "$output" = 245c30303178 ]
      fi
      ;;
    *) false ;;
  esac
}

@test "@A of a sparse array round-trips; the quoted [@] form splits and the bare name keeps only [0]" {
  run_lab_c 'a=([0]=first [3]="$(printf "\044(echo pwned)")" [7]=q); s=${a[*]@A}; printf "%s\n" "$s"; set -- "${a[@]@A}"; n=$#; t=${a@A}; unset a; eval "$s"; printf "%s|%s|%s|%s\n" "${!a[*]}" "${a[3]}" "$n" "$t"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "${lines[0]}" = 'declare -a a=([0]="first" [3]="\$(echo pwned)" [7]="q")' ]
        [ "${lines[1]}" = "0 3 7|\$(echo pwned)|3|declare -a a='first'" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@A of scalars: attributes become a declare command, a local in 5.3 gains declare" {
  run_lab_c 'x="a b"; declare -i n=5; declare -rx R=1; declare -i u; f() { local l=1; printf "%s\n" "${l@A}"; }; printf "%s\n" "${x@A}" "${n@A}" "${R@A}" "[${u@A}]"; f'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "${lines[0]}" = "x='a b'" ]
        [ "${lines[1]}" = "declare -i n='5'" ]
        [ "${lines[2]}" = "declare -rx R='1'" ]
      fi
      if [ "$BV" -ge 53 ]; then
        [ "${lines[3]}" = "[declare -i u]" ]
        [ "${lines[4]}" = "declare l='1'" ]
      elif [ "$BV" -ge 51 ]; then
        [ "${lines[3]}" = "[declare -i u]" ]
        [ "${lines[4]}" = "l='1'" ]
      elif [ "$BV" -ge 44 ]; then
        [ "${lines[3]}" = "[]" ]
        [ "${lines[4]}" = "l='1'" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@a prints attribute letters, follows a nameref, and sees unset variables from 5.0" {
  run_lab_c 'declare -rix n=1; declare -i t=1; declare -n r=t; declare -i u; x=1; printf "%s|%s|%s|%s\n" "${n@a}" "${r@a}" "${u@a}" "${x@a}"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 50 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "irx|i|i|" ]
      elif [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "irx|i||" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@E decodes \$'...' escapes; 4.4 doubles a 0x01 it decodes, 5.1 and 5.2 drop a raw one" {
  run_lab_c 'x="a\tb\x41\101\cA\q"; printf %s "${x@E}" | od -An -tx1 | tr -d " \n"; printf "\n"; x="\x01"; y=${x@E}; printf "%s\n" "${#y}"; x=$(printf "\001\001"); y=${x@E}; printf "%s\n" "${#y}"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 53 ]; then
        [ "$output" = "$(printf '6109624141015c71\n1\n2')" ]
      elif [ "$BV" -ge 51 ]; then
        [ "$output" = "$(printf '6109624141015c71\n1\n1')" ]
      elif [ "$BV" -ge 44 ]; then
        [ "$output" = "$(printf '610962414101015c71\n2\n2')" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@P runs command substitution in the value unless promptvars is off; POSIX mode ignores promptvars" {
  run_lab_c 'if [ -o posix ]; then echo posix; else echo default; fi; z=$(printf "\044(echo ran >%s)ok" "$1"); printf "%s\n" "${z@P}"; shopt -u promptvars; z=$(printf "\044(echo ran >%s)ok" "$2"); printf "%s\n" "${z@P}"' sh "$BATS_TEST_TMPDIR/on" "$BATS_TEST_TMPDIR/off"
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "${lines[1]}" = ok ]
        on=$(cat "$BATS_TEST_TMPDIR/on")
        [ "$on" = ran ]
        case ${lines[0]} in
          default)
            [ "${lines[2]}" = "\$(echo ran >$BATS_TEST_TMPDIR/off)ok" ]
            [ ! -e "$BATS_TEST_TMPDIR/off" ]
            ;;
          posix)
            # decode_prompt_string expands when promptvars || posixly_correct.
            [ "${lines[2]}" = ok ]
            [ -e "$BATS_TEST_TMPDIR/off" ]
            ;;
          *) false ;;
        esac
      else
        [ "$status" -ne 0 ]
        [ ! -e "$BATS_TEST_TMPDIR/on" ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      [ ! -e "$BATS_TEST_TMPDIR/on" ]
      ;;
    *) false ;;
  esac
}

@test "case modification: ^ and , from bash 4.0, ~ toggles, a pattern limits which characters change" {
  run_lab_c 'x="hello World"; printf "%s|" "${x^}" "${x^^}" "${x,}" "${x,,}" "${x~}" "${x~~}" "${x^^[lo]}"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "Hello World|HELLO WORLD|hello World|hello world|Hello World|HELLO wORLD|heLLO WOrLd|" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@U, @u and @L (bash 5.1) map case through the locale" {
  run_lab_c 'export LC_ALL=C.UTF-8; x="hello World"; e=$(printf "\303\251"); printf "%s|" "${x@U}" "${x@u}" "${x@L}"; printf "%s" "${e@U}" | od -An -tx1 | tr -d " \n"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 51 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "HELLO WORLD|Hello World|hello world|c389" ]
      else
        [ "$status" -ne 0 ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@K quotes key-value pairs (5.1); an associative @K ends in a space" {
  run_lab_c 'declare -A h=([k1]="v 1"); a=(x "y z"); printf "[%s]\n" "${h[*]@K}" "${a[*]@K}"; eval "declare -A d=(${h[*]@K})"; printf "[%s]\n" "${d[k1]}"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 51 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "$(printf '[k1 "v 1" ]\n[0 "x" 1 "y z"]\n[v 1]')" ]
      elif [ "$BV" -ge 44 ]; then
        [ "$status" -ne 0 ]
        [[ $output == *'${h[*]@K}: bad substitution'* ]]
      else
        # bash 3.2 drops the @K after the subscript and prints the bare elements.
        [[ $output == *"[x y z]"* ]]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "@k (5.2) yields separate words, but not inside an associative compound assignment" {
  run_lab_c 'declare -A h=([k1]="v 1"); set -- "${h[@]@k}"; printf "%s\n" "$#"; declare -A c=("${h[@]@k}"); printf "[%s]\n" "${!c[*]}"'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 52 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "$(printf '2\n[k1 v 1]')" ]
      elif [ "$BV" -ge 44 ]; then
        [ "$status" -ne 0 ]
        [[ $output == *'${h[@]@k}: bad substitution'* ]]
      else
        [ "$output" != "$(printf '2\n[k1 v 1]')" ]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "zsh spells the same transformations as flags: (qq) and (U)" {
  run_lab_c 'x=$(printf "it\047s a"); printf "%s\n" "${(qq)x}" "${(U)x}"'
  case $FAMILY in
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "$output" = "$(printf "'it'\\\\''s a'\nIT'S A")" ]
      ;;
    bash | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -ne 0 ]
      ;;
    *) false ;;
  esac
}

@test "worked example: requote.sh output re-creates the arguments when parsed again" {
  run_lab "$LAB_DIR/requote.sh" printf '<%s>' 'a b' "$(printf 'c\nd')" "it's" '' '$(echo pwned)'
  case $FAMILY in
    bash)
      if [ "$BV" -ge 44 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "'printf' '<%s>' 'a b' \$'c\\nd' 'it'\\''s' '' '\$(echo pwned)'" ]
        run_lab_c "$output"
        [ "$status" -eq 0 ]
        [ "$output" = "$(printf '<a b><c\nd><it'"'"'s><><$(echo pwned)>')" ]
      else
        [ "$status" -eq 2 ]
        [[ $output == "requote.sh: this shell has no \${*@Q} (BASH_VERSION=3.2."* ]]
      fi
      ;;
    zsh | zsh-sh | dash | posh | busybox | yash | ksh93 | mksh)
      [ "$status" -eq 2 ]
      [ "$output" = 'requote.sh: this shell has no ${*@Q} (BASH_VERSION=unset)' ]
      ;;
    *) false ;;
  esac
}
