#!/usr/bin/env bats
# Tests for associative arrays: which shells have them, how keys are stored and walked,
# and which subscript forms evaluate code hidden in a key, per shell family and bash release.
# shellcheck disable=SC2016 # single-quoted code and data belong to the shell under test, never to bats

setup() {
  load ../lib/lab
  COUNT_KEYS="$BATS_TEST_DIRNAME/count-keys.sh"
  FAMILY=$(lab_shell_family)
  VERSION=$(lab_shell_version)
  cd "$BATS_TEST_TMPDIR" || return 1
}

# bash_level: print the bash release as major and minor digits (5.3.15 -> 53), or 0 for other shells.
bash_level() {
  local major rest
  if [ "$FAMILY" != bash ]; then
    echo 0
    return
  fi
  major=${VERSION%%.*}
  rest=${VERSION#*.}
  printf '%s%s\n' "$major" "${rest%%.*}"
}

# ksh_release: print m for ksh93u+m, 2012 for ksh93u+ 2012-08-01, or unknown.
ksh_release() {
  case $VERSION in
    *93u+m*) echo m ;;
    *"93u+ 2012"*) echo 2012 ;;
    *) echo unknown ;;
  esac
}

# marker_lines: print how many times the code hidden in a key ran (each run appends one line).
marker_lines() {
  local n
  if [ -f marker ]; then
    n=$(wc -l <marker)
    echo "$((n))"
  else
    echo 0
  fi
}

# set_last: set last to the last line of the last run's output.
set_last() {
  last=${lines[${#lines[@]} - 1]}
}

# The key that runs code when a shell evaluates it a second time; each run appends to ./marker.
HOSTILE_PROLOGUE=$(
  cat <<'EOF'
typeset -A m 2>/dev/null || { echo none; exit 0; }
k='$(echo ran >>marker)'
EOF
)

# --- which shells have associative arrays at all ---

@test "typeset -A exists in bash 4.0 and later, zsh and ksh93 only" {
  run_lab_c 'typeset -A m 2>/dev/null || { echo none; exit 0; }; m[k]=v; m[two]=2; echo "${m[k]} ${#m[@]}"'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53) [ "$output" = "v 2" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93) [ "$output" = "v 2" ] ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "without typeset -A, a[word]=v makes an indexed array and evaluates word as arithmetic" {
  run_lab_c 'foo=7; a[foo]=x 2>/dev/null || { echo "status $?"; exit 0; }; eval '\''echo "${#a[@]} ${a[7]}"'\'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash | mksh | ksh93 | posh) [ "$output" = "1 x" ] ;;
    zsh) [ "$output" = "7 x" ] ;;    # zsh arrays start at 1, so elements 1 to 6 are created empty
    zsh-sh) [ "$output" = "8 x" ] ;; # sh emulation sets KSH_ARRAYS: indices start at 0
    dash | busybox | yash) [ "$output" = "status 127" ] ;;
    *) false ;;
  esac
}

# --- how keys are stored ---

@test "an associative subscript is a string: m[1+1] is not m[2]" {
  run_lab_c 'typeset -A m 2>/dev/null || { echo none; exit 0; }; m[1+1]=x; echo "${m[1+1]-unset} ${m[2]-unset}"'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53) [ "$output" = "x unset" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93) [ "$output" = "x unset" ] ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "bash rejects an empty key and drops the rest of that input line; zsh and ksh93 store it" {
  code=$(
    cat <<'EOF'
typeset -A m 2>/dev/null || { echo none; exit 0; }
if [ -n "${BASH_VERSION-}" ] && [ -o posix ]; then echo "posix mode"; fi
k=
m[$k]=e; echo "same line"
echo "status $? count ${#m[@]}"
k=@
m[$k]=at
echo "at=${m[$k]}"
EOF
  )
  run_lab_c "$code"
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53)
          if [ "${lines[0]}" = "posix mode" ]; then
            # In POSIX mode the assignment error ends the shell (bash as sh, or bash --posix).
            [ "$status" -eq 127 ]
            [[ ${lines[1]} == *"m[\$k]: bad array subscript" ]]
            [ "${#lines[@]}" -eq 2 ]
            return
          fi
          [ "$status" -eq 0 ]
          [[ ${lines[0]} == *"m[\$k]: bad array subscript" ]]
          [ "${lines[1]}" = "status 1 count 0" ]
          [ "${lines[2]}" = "at=at" ]
          [ "${#lines[@]}" -eq 3 ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "same line" ]
      [ "${lines[1]}" = "status 0 count 1" ]
      [ "${lines[2]}" = "at=at" ]
      ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

# --- the order in which keys come back ---

@test "the key order depends on the set of keys, not on the order of insertion" {
  code=$(
    cat <<'EOF'
typeset -A m 2>/dev/null || { echo none; exit 0; }
typeset -A n
for k in alpha bravo charlie delta echo; do m[$k]=1; done
for k in echo delta charlie bravo alpha; do n[$k]=1; done
if [ -n "${ZSH_VERSION-}" ]; then
  eval 'echo "${(k)m[@]}"; echo "${(k)n[@]}"'
else
  eval 'echo "${!m[@]}"; echo "${!n[@]}"'
fi
EOF
  )
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44) expect="bravo alpha delta echo charlie" ;;           # FNV-1 with the pre-5.0 seed
        51 | 52 | 53) expect="alpha echo delta bravo charlie" ;; # seed corrected in bash 5.0
        *) false ;;
      esac
      ;;
    zsh | zsh-sh) expect="charlie delta echo bravo alpha" ;;
    ksh93) expect="alpha bravo charlie delta echo" ;; # an ordered set: byte order of the keys
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
  if [ -n "${expect-}" ]; then
    [ "${lines[0]}" = "$expect" ]
    [ "${lines[1]}" = "$expect" ]
  fi
}

@test "bash 5.1 and later grow the table past 2048 keys, which reorders the keys already there" {
  code=$(
    cat <<'EOF'
[ -n "${BASH_VERSION-}" ] || { echo "not bash"; exit 0; }
declare -A m 2>/dev/null || { echo none; exit 0; }
for k in alpha bravo charlie delta echo; do m[$k]=1; done
echo "${!m[@]}"
i=0; while [ "$i" -lt 2100 ]; do m[f$i]=1; i=$((i + 1)); done
i=0; while [ "$i" -lt 2100 ]; do unset "m[f$i]"; i=$((i + 1)); done
echo "${#m[@]}: ${!m[@]}"
EOF
  )
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44)
          # Fixed 128 buckets: the same walk before and after.
          [ "${lines[0]}" = "bravo alpha delta echo charlie" ]
          [ "${lines[1]}" = "5: bravo alpha delta echo charlie" ]
          ;;
        51 | 52 | 53)
          # 1024 -> 4096 buckets at the 2048th key, and the table never shrinks back.
          [ "${lines[0]}" = "alpha echo delta bravo charlie" ]
          [ "${lines[1]}" = "5: echo alpha delta bravo charlie" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93 | mksh | yash | posh | busybox | dash) [ "$output" = "not bash" ] ;;
    *) false ;;
  esac
}

# --- subscripts evaluated twice: code hidden in a key ---

@test "(( m[\$k]++ )) runs code in the key in bash 5.1 and older, zsh, and ksh93u+ 2012" {
  run_lab_c "$HOSTILE_PROLOGUE"'
m[$k]=1
(( m[$k]++ ))
echo "value ${m[$k]}"'
  [ "$status" -eq 0 ]
  ran=$(marker_lines)
  set_last
  case $FAMILY in
    bash)
      case $(bash_level) in
        32)
          [ "$output" = none ]
          [ "$ran" = 0 ]
          ;;
        44 | 51)
          [ "$ran" = 2 ]
          [ "$last" = "value 1" ]
          ;;
        52 | 53)
          [ "$ran" = 0 ]
          [ "$output" = "value 2" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh)
      [ "$ran" = 2 ]
      [ "$last" = "value 1" ]
      ;;
    ksh93)
      case $(ksh_release) in
        m)
          [ "$ran" = 0 ]
          [ "$output" = "value 2" ]
          ;;
        2012)
          [ "$ran" = 1 ]
          [ "$last" = "value 1" ]
          ;;
        *) false ;;
      esac
      ;;
    mksh | yash | posh | busybox | dash)
      [ "$output" = none ]
      [ "$ran" = 0 ]
      ;;
    *) false ;;
  esac
}

@test "[[ -v m[\$k] ]] runs code in the key in bash 5.1 and older and in zsh" {
  run_lab_c "$HOSTILE_PROLOGUE"'
m[$k]=1
[[ -v m[$k] ]]
echo "status $?"'
  [ "$status" -eq 0 ]
  ran=$(marker_lines)
  set_last
  case $FAMILY in
    bash)
      case $(bash_level) in
        32)
          [ "$output" = none ]
          [ "$ran" = 0 ]
          ;;
        44 | 51)
          [ "$ran" = 1 ]
          [ "$last" = "status 1" ]
          ;;
        52 | 53)
          [ "$ran" = 0 ]
          [ "$output" = "status 0" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh)
      [ "$ran" = 1 ]
      [ "$last" = "status 1" ] # the code ran and printed nothing, so -v tested the empty key
      ;;
    ksh93)
      [ "$ran" = 0 ]
      [ "$output" = "status 0" ]
      ;;
    mksh | yash | posh | busybox | dash)
      [ "$output" = none ]
      [ "$ran" = 0 ]
      ;;
    *) false ;;
  esac
}

@test "unset \"m[\$k]\" runs code in the key in every bash release; zsh and ksh93 remove the key" {
  run_lab_c "$HOSTILE_PROLOGUE"'
m[$k]=1; m[other]=2
unset "m[$k]"
echo "count ${#m[@]}"'
  ran=$(marker_lines)
  set_last
  case $FAMILY in
    bash)
      case $(bash_level) in
        32)
          [ "$output" = none ]
          [ "$ran" = 0 ]
          ;;
        44 | 51 | 52 | 53)
          [ "$ran" = 1 ]
          [ "$last" = "count 2" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93)
      [ "$ran" = 0 ]
      [ "$output" = "count 1" ]
      ;;
    mksh | yash | posh | busybox | dash)
      [ "$output" = none ]
      [ "$ran" = 0 ]
      ;;
    *) false ;;
  esac
}

@test "unset 'm[\$k]' is the safe form in bash and removes nothing in zsh and ksh93" {
  run_lab_c "$HOSTILE_PROLOGUE"'
m[$k]=1; m[other]=2
unset '\''m[$k]'\''
echo "status $? count ${#m[@]}"'
  ran=$(marker_lines)
  [ "$ran" = 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53) [ "$output" = "status 0 count 1" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh) [ "$output" = "status 0 count 2" ] ;; # removes the key spelled $k, which is absent
    ksh93) [ "$output" = "status 1 count 2" ] ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "the old (( m[\\\$k]++ )) workaround updates the wrong key from bash 5.2 on" {
  code=$(
    cat <<'EOF'
typeset -A m 2>/dev/null || { echo none; exit 0; }
k='x y'
m[$k]=1
(( m[\$k]++ ))
lit='$k'
echo "value ${m[$k]} literal ${m[$lit]-unset}"
EOF
  )
  run_lab_c "$code"
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51) [ "$output" = "value 2 literal unset" ] ;;
        52 | 53) [ "$output" = "value 1 literal 1" ] ;; # the key is now the two characters $k
        *) false ;;
      esac
      ;;
    zsh | zsh-sh) [ "$output" = "value 2 literal unset" ] ;;
    ksh93)
      case $(ksh_release) in
        m) [ "$output" = "value 1 literal 1" ] ;; # like bash 5.2: the subscript is no longer expanded twice
        2012) [ "$output" = "value 2 literal unset" ] ;;
        *) false ;;
      esac
      ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "the version-portable forms: \${m[\$k]+set} and m[\$k]=\$(( \${m[\$k]} + 1 )) never run the key" {
  run_lab_c "$HOSTILE_PROLOGUE"'
m[$k]=1
[ -n "${m[$k]+set}" ] && echo member
[ -n "${m[nokey]+set}" ] || echo "not member"
m[$k]=$(( ${m[$k]} + 1 ))
echo "value ${m[$k]} count ${#m[@]}"'
  [ "$status" -eq 0 ]
  ran=$(marker_lines)
  [ "$ran" = 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53) [ "$output" = "$(printf 'member\nnot member\nvalue 2 count 1')" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93) [ "$output" = "$(printf 'member\nnot member\nvalue 2 count 1')" ] ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "let 'm[\$k]++' single-quoted is safe in bash 4.4 to 5.3 and zsh, and misses the key in ksh93u+m" {
  run_lab_c "$HOSTILE_PROLOGUE"'
m[$k]=1
let '\''m[$k]++'\''
echo "value ${m[$k]} count ${#m[@]}"'
  ran=$(marker_lines)
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53)
          [ "$ran" = 0 ]
          [ "$output" = "value 2 count 1" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh)
      [ "$ran" = 0 ]
      [ "$output" = "value 2 count 1" ]
      ;;
    ksh93)
      [ "$ran" = 0 ]
      case $(ksh_release) in
        m) [ "$output" = "value 1 count 2" ] ;; # let increments a new key spelled $k
        2012) [ "$output" = "value 2 count 1" ] ;;
        *) false ;;
      esac
      ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "an unquoted unset m[1] is a glob: a file named m1 turns it into unset m1" {
  : >m1
  run_lab_c 'typeset -A m 2>/dev/null || { echo none; exit 0; }; m[1]=one; m1=scalar; unset m[1]; echo "count ${#m[@]} m1=${m1-unset}"'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53) [ "$output" = "count 1 m1=unset" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93) [ "$output" = "count 1 m1=unset" ] ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "shopt -s assoc_expand_once makes all three forms safe in bash 5.0 and later" {
  code=$(
    cat <<'EOF'
shopt -s assoc_expand_once 2>/dev/null || { echo "no option"; exit 0; }
if shopt -s array_expand_once 2>/dev/null; then echo "array_expand_once: yes"; else echo "array_expand_once: no"; fi
declare -A m
k='$(echo ran >>marker)'
m[$k]=1
(( m[$k]++ )); echo "arith $? value ${m[$k]}"
[[ -v m[$k] ]]; echo "test $?"
unset "m[$k]"; echo "unset $? count ${#m[@]}"
EOF
  )
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  ran=$(marker_lines)
  [ "$ran" = 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32 | 44) [ "$output" = "no option" ] ;;
        51 | 52 | 53)
          case $(bash_level) in
            53) [ "${lines[0]}" = "array_expand_once: yes" ] ;; # the 5.3 name; assoc_expand_once stays a synonym
            *) [ "${lines[0]}" = "array_expand_once: no" ] ;;
          esac
          [ "${lines[1]}" = "arith 0 value 2" ]
          [ "${lines[2]}" = "test 0" ]
          [ "${lines[3]}" = "unset 0 count 0" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93 | mksh | yash | posh | busybox | dash) [ "$output" = "no option" ] ;;
    *) false ;;
  esac
}

# --- assignment, copying, scope, and export ---

@test "key-value list assignment m=(k1 v1 k2 v2) works in bash 5.1 and later and in zsh" {
  run_lab_c 'typeset -A m 2>/dev/null || { echo none; exit 0; }; eval "m=(k1 v1 k2 v2)" 2>/dev/null; echo "${m[k1]-unset} ${#m[@]}"'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44) [ "$output" = "unset 0" ] ;; # each word: "must use subscript when assigning associative array"
        51 | 52 | 53) [ "$output" = "v1 2" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh) [ "$output" = "v1 2" ] ;;
    ksh93) [ "$output" = "unset 0" ] ;; # "cannot append index(ed) array to associative array"
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "typeset -p prints an assignment that re-creates hostile keys exactly" {
  code=$(
    cat <<'EOF'
typeset -A m 2>/dev/null || { echo none; exit 0; }
k1='$(echo ran >>marker)'; k2='a]b'; k3='x y'; k4="it's"
m[$k1]=1; m[$k2]=2; m[$k3]=3; m[$k4]=4
s=$(typeset -p m)
unset m
eval "$s"
echo "count ${#m[@]} values ${m[$k1]-U}${m[$k2]-U}${m[$k3]-U}${m[$k4]-U}"
EOF
  )
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  ran=$(marker_lines)
  [ "$ran" = 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53) [ "$output" = "count 4 values 1234" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93) [ "$output" = "count 4 values 1234" ] ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "copying: eval of @K works from bash 5.1, a @k list collapses into one key, a loop always works" {
  # The body travels in the environment: posh parses a whole -c string before running any of it,
  # so it would reject these compound assignments even though the guard skips them.
  COPY_BODY=$(
    cat <<'EOF'
k1='x y'; k2='$(echo ran >>marker)'
a[$k1]=1; a[$k2]=2; a[k]=''
# A subshell each: an unknown transformation is a fatal "bad substitution" in a non-interactive bash.
(eval "declare -A b=(${a[@]@K})"; echo "K ${#b[@]} ${b[$k1]-U}${b[$k2]-U}${b[k]+set}") 2>/dev/null || echo "K unsupported"
(declare -A c; c=("${a[@]@k}"); echo "k ${#c[@]}") 2>/dev/null || echo "k unsupported"
declare -A d
for key in "${!a[@]}"; do d[$key]=${a[$key]}; done
echo "loop ${#d[@]} ${d[$k1]-U}${d[$k2]-U}${d[k]+set}"
EOF
  )
  export COPY_BODY
  run_lab_c '[ -n "${BASH_VERSION-}" ] || { echo "not bash"; exit 0; }; declare -A a 2>/dev/null || { echo none; exit 0; }; eval "$COPY_BODY"'
  [ "$status" -eq 0 ]
  ran=$(marker_lines)
  [ "$ran" = 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44)
          [ "${lines[0]}" = "K unsupported" ]
          [ "${lines[1]}" = "k 0" ] # "must use subscript when assigning associative array"
          [ "${lines[2]}" = "loop 3 12set" ]
          ;;
        51)
          [ "${lines[0]}" = "K 3 12set" ]
          [ "${lines[1]}" = "k unsupported" ]
          [ "${lines[2]}" = "loop 3 12set" ]
          ;;
        52 | 53)
          [ "${lines[0]}" = "K 3 12set" ]
          [ "${lines[1]}" = "k 1" ] # the expanded words are joined into a single key
          [ "${lines[2]}" = "loop 3 12set" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93 | mksh | yash | posh | busybox | dash) [ "$output" = "not bash" ] ;;
    *) false ;;
  esac
}

@test "converting an indexed array: bash refuses, zsh empties it, ksh93 keeps its elements as keys" {
  code=$(
    cat <<'EOF'
typeset -A probe 2>/dev/null || { echo none; exit 0; }
eval 'a=(one two)'
typeset -A a 2>/dev/null
echo "status $? count ${#a[@]} zero=${a[0]-unset}"
EOF
  )
  run_lab_c "$code"
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53) [ "$output" = "status 1 count 2 zero=one" ] ;; # still an indexed array
        *) false ;;
      esac
      ;;
    zsh | zsh-sh) [ "$output" = "status 0 count 0 zero=unset" ] ;;
    ksh93) [ "$output" = "status 0 count 2 zero=one" ] ;; # key "0" now holds "one"
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "typeset -A in a function is local, except in a ksh93 name() function" {
  code=$(
    cat <<'EOF'
typeset -A probe 2>/dev/null || { echo none; exit 0; }
f() { typeset -A l; l[x]=1; }
eval 'function g { typeset -A l2; l2[x]=1; }'
f; g
echo "name()=${l[x]-unset} function=${l2[x]-unset}"
EOF
  )
  run_lab_c "$code"
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53) [ "$output" = "name()=unset function=unset" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh) [ "$output" = "name()=unset function=unset" ] ;;
    ksh93) [ "$output" = "name()=1 function=unset" ] ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "an associative array cannot be exported to a child process" {
  run_lab_c 'typeset -A m 2>/dev/null || { echo none; exit 0; }; m[k]=v; m[0]=zero; export m; /bin/sh -c '\''echo "child sees ${m-nothing}"'\'
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$output" = none ] ;;
        44 | 51 | 52 | 53) [ "$output" = "child sees nothing" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93) [ "$output" = "child sees nothing" ] ;;
    mksh | yash | posh | busybox | dash) [ "$output" = none ] ;;
    *) false ;;
  esac
}

@test "under set -u a missing key is an error that ends the shell: status 127 in bash, 1 in zsh and ksh93" {
  # bats warns (BW01) when the shell under test exits 127; that status is the behavior asserted here.
  run_lab_c 'typeset -A m 2>/dev/null || { echo none; exit 0; }; set -u; m[k]=v; echo "${m[nokey]-default}"; echo "${m[nokey]}"; echo survived'
  case $FAMILY in
    bash)
      case $(bash_level) in
        32)
          [ "$status" -eq 0 ]
          [ "$output" = none ]
          ;;
        44 | 51 | 52 | 53)
          [ "${lines[0]}" = default ]
          [[ ${lines[1]} == *"m[nokey]: unbound variable" ]]
          [ "$status" -eq 127 ]
          [ "${#lines[@]}" -eq 2 ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93)
      [ "${lines[0]}" = default ]
      [[ ${lines[1]} == *"parameter not set" ]]
      [ "$status" -eq 1 ]
      [ "${#lines[@]}" -eq 2 ]
      ;;
    mksh | yash | posh | busybox | dash)
      [ "$status" -eq 0 ]
      [ "$output" = none ]
      ;;
    *) false ;;
  esac
}

# --- the worked example ---

# write_input: write the ten-line input with every key that breaks a naive script:
# duplicates, two empty lines (one of them last), *, @, ], a space, and $(...).
write_input() {
  printf '%s\n' apple '$(echo ran >>marker)' '*' '@' 'a]b' '' 'x y' apple '*' '' >input
}

@test "count-keys.sh counts hostile lines exactly and runs none of them" {
  write_input
  run_lab "$COUNT_KEYS" <input
  ran=$(marker_lines)
  [ "$ran" = 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32)
          [ "$status" -eq 2 ]
          [[ $output == "count-keys: needs bash 4.0 or later (this shell: 3.2."* ]]
          ;;
        44 | 51 | 52 | 53)
          [ "$status" -eq 0 ]
          expected=$(printf '2\t\n2\t*\n2\tapple\n1\t$(echo ran >>marker)\n1\t@\n1\ta]b\n1\tx y')
          [ "$output" = "$expected" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93 | mksh | yash | posh | busybox | dash)
      [ "$status" -eq 2 ]
      [ "$output" = "count-keys: needs bash 4.0 or later (this shell: not bash)" ]
      ;;
    *) false ;;
  esac
}

@test "count-keys.sh --drop removes a hostile key without running it" {
  write_input
  run_lab "$COUNT_KEYS" --drop '$(echo ran >>marker)' <input
  ran=$(marker_lines)
  [ "$ran" = 0 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [ "$status" -eq 2 ] ;;
        44 | 51 | 52 | 53)
          [ "$status" -eq 0 ]
          expected=$(printf '2\t\n2\t*\n2\tapple\n1\t@\n1\ta]b\n1\tx y')
          [ "$output" = "$expected" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93 | mksh | yash | posh | busybox | dash) [ "$status" -eq 2 ] ;;
    *) false ;;
  esac
}

@test "count-keys.sh --drop of * or the empty line removes only that key" {
  write_input
  run_lab "$COUNT_KEYS" --drop '*' <input
  star_status=$status star_output=$output
  run_lab "$COUNT_KEYS" --drop '' <input
  case $FAMILY in
    bash)
      case $(bash_level) in
        32)
          [ "$star_status" -eq 2 ]
          [ "$status" -eq 2 ]
          ;;
        44 | 51 | 52 | 53)
          [ "$star_status" -eq 0 ]
          [ "$status" -eq 0 ]
          expected=$(printf '2\t\n2\tapple\n1\t$(echo ran >>marker)\n1\t@\n1\ta]b\n1\tx y')
          [ "$star_output" = "$expected" ]
          expected=$(printf '2\t*\n2\tapple\n1\t$(echo ran >>marker)\n1\t@\n1\ta]b\n1\tx y')
          [ "$output" = "$expected" ]
          ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93 | mksh | yash | posh | busybox | dash)
      [ "$star_status" -eq 2 ]
      [ "$status" -eq 2 ]
      ;;
    *) false ;;
  esac
}

@test "count-keys.sh rejects a bad usage with status 2" {
  run_lab "$COUNT_KEYS" --bogus </dev/null
  [ "$status" -eq 2 ]
  case $FAMILY in
    bash)
      case $(bash_level) in
        32) [[ $output == "count-keys: needs bash 4.0 or later"* ]] ;;
        44 | 51 | 52 | 53) [ "$output" = "usage: count-keys.sh [--drop LINE] <input" ] ;;
        *) false ;;
      esac
      ;;
    zsh | zsh-sh | ksh93 | mksh | yash | posh | busybox | dash) [[ $output == "count-keys: needs bash 4.0 or later"* ]] ;;
    *) false ;;
  esac
}
