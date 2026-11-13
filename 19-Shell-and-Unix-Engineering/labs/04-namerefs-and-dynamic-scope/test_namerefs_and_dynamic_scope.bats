#!/usr/bin/env bats
# Namerefs (typeset -n) and dynamic scope: what each shell binds, shadows, unsets, and exports.

setup() {
  load ../lib/lab
  LAB_DIR=$BATS_TEST_DIRNAME
  FAMILY=$(lab_shell_family)
  VERSION=$(lab_shell_version)
}

# output_has TEXT: succeed when the captured output contains TEXT.
output_has() {
  case $output in
    *"$1"*) ;;
    *) false ;;
  esac
}

# output_lacks TEXT: succeed when the captured output does not contain TEXT.
output_lacks() {
  case $output in
    *"$1"*) false ;;
    *) ;;
  esac
}

# bash_at_least MAJOR MINOR: succeed when the bash under test is MAJOR.MINOR or newer.
bash_at_least() {
  local major=${VERSION%%.*} rest minor
  rest=${VERSION#*.}
  minor=${rest%%[!0-9]*}
  [ "$major" -gt "$1" ] || { [ "$major" -eq "$1" ] && [ "$minor" -ge "$2" ]; }
}

# nameref_kind: print which nameref implementation the shell under test has:
# bash (4.3 or newer), ksh93, mksh, none (no typeset -n at all), or unknown.
nameref_kind() {
  case $FAMILY in
    bash)
      if bash_at_least 4 3; then echo bash; else echo none; fi
      ;;
    ksh93) echo ksh93 ;;
    mksh) echo mksh ;;
    dash | posh | busybox | yash) echo none ;;
    zsh | zsh-sh)
      # typeset -n is in zsh development sources for the release after 5.9; no 5.9.x has it
      case $VERSION in
        [1-4].* | 5.[0-9] | 5.[0-9].*) echo none ;;
        *) echo unknown ;;
      esac
      ;;
    *) echo unknown ;;
  esac
}

# assert_unsupported: the probe found no typeset -n and said so.
assert_unsupported() {
  [ "$status" -eq 3 ]
  [ "$output" = "namerefs: unsupported" ]
}

@test "support: typeset -n exists in bash 4.3+, ksh93 and mksh only" {
  run_lab "$LAB_DIR/nameref_probe.sh" support
  [ "$status" -eq 0 ]
  case $(nameref_kind) in
    bash | ksh93 | mksh) [ "$output" = "namerefs=yes" ] ;;
    none) [ "$output" = "namerefs=no" ] ;;
    *) false ;;
  esac
}

@test "outparam naive: a nameref writes the caller's variable when no local shares its name" {
  run_lab "$LAB_DIR/outparam.sh" naive result
  case $(nameref_kind) in
    bash | ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "status=0" ]
      [ "${lines[1]}" = "result=9" ]
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "outparam naive: a callee local with the caller's name captures the write, except in ksh93" {
  run_lab "$LAB_DIR/outparam.sh" naive best
  case $(nameref_kind) in
    bash | mksh)
      # the name is resolved at use, under dynamic scope, so it finds naive_max's own best
      [ "${lines[0]}" = "status=0" ]
      [ "${lines[1]}" = "best=preset" ]
      ;;
    ksh93)
      # typeset -n ref=$1 in a function NAME { } body binds to the caller's variable at declaration
      [ "${lines[0]}" = "status=0" ]
      [ "${lines[1]}" = "best=9" ]
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "outparam guarded: private local prefix reaches the caller's best and refuses _gm_ names" {
  run_lab "$LAB_DIR/outparam.sh" guarded best
  case $(nameref_kind) in
    bash | ksh93 | mksh)
      [ "${lines[0]}" = "status=0" ]
      [ "${lines[1]}" = "best=9" ]
      run_lab "$LAB_DIR/outparam.sh" guarded _gm_best
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "guarded_max: reserved name: _gm_best" ]
      [ "${lines[1]}" = "status=2" ]
      [ "${lines[2]}" = "_gm_best=preset" ]
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "outparam naive: a subscript in the target name runs a command substitution in bash and mksh" {
  cd "$BATS_TEST_TMPDIR"
  # shellcheck disable=SC2016 # the payload is data for the shell under test
  run_lab "$LAB_DIR/outparam.sh" naive 'a[$(touch pwned)0]'
  case $(nameref_kind) in
    bash | mksh)
      [ "$status" -eq 0 ]
      [ "$output" = "status=0" ]
      [ -e pwned ]
      ;;
    ksh93)
      # ksh93 resolves the subscript as arithmetic at declaration and refuses $(...) there
      [ "$status" -eq 0 ]
      output_has "arithmetic syntax error"
      output_has "status=1"
      [ ! -e pwned ]
      ;;
    none)
      assert_unsupported
      [ ! -e pwned ]
      ;;
    *) false ;;
  esac
}

@test "outparam guarded and portable: the identifier check rejects the payload before any expansion" {
  cd "$BATS_TEST_TMPDIR"
  # shellcheck disable=SC2016 # the payload is data for the shell under test
  run_lab "$LAB_DIR/outparam.sh" portable 'a[$(touch pwned)0]'
  [ "$status" -eq 0 ]
  output_has "portable_max: not an identifier"
  output_has "status=2"
  [ ! -e pwned ]
  # shellcheck disable=SC2016 # the payload is data for the shell under test
  run_lab "$LAB_DIR/outparam.sh" guarded 'a[$(touch pwned)0]'
  case $(nameref_kind) in
    bash | ksh93 | mksh)
      [ "$status" -eq 0 ]
      output_has "guarded_max: not an identifier"
      output_has "status=2"
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
  [ ! -e pwned ]
}

@test "outparam portable: the validated eval fallback returns into any name in every shell" {
  run_lab "$LAB_DIR/outparam.sh" portable best
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "status=0" ]
  [ "${lines[1]}" = "best=9" ]
  run_lab "$LAB_DIR/outparam.sh" portable portable_max_best
  [ "$status" -eq 0 ]
  [ "${lines[0]}" = "portable_max: reserved name: portable_max_best" ]
  [ "${lines[1]}" = "status=2" ]
  [ "${lines[2]}" = "portable_max_best=preset" ]
}

@test "chain: bash resolves at most 8 nameref links, ksh93 and mksh resolve 9" {
  run_lab "$LAB_DIR/nameref_probe.sh" chain 8
  case $(nameref_kind) in
    bash | ksh93 | mksh) [ "$output" = "depth=8 value=hit" ] ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
  run_lab "$LAB_DIR/nameref_probe.sh" chain 9
  case $(nameref_kind) in
    bash)
      # NAMEREF_MAX is 8 in variables.h; bash 5.3 also warns, older releases fail silently
      [ "$status" -eq 0 ]
      output_has "depth=9 value=UNSET"
      if bash_at_least 5 3; then
        output_has "maximum nameref depth (8) exceeded"
      else
        [ "$output" = "depth=9 value=UNSET" ]
      fi
      ;;
    ksh93 | mksh) [ "$output" = "depth=9 value=hit" ] ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "bind: bash and mksh evaluate a nameref subscript at each use, ksh93 once at declaration" {
  run_lab "$LAB_DIR/nameref_probe.sh" bind
  case $(nameref_kind) in
    bash | mksh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "i=0 e=zero" ]
      [ "${lines[1]}" = "i=2 e=two" ]
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "i=0 e=zero" ]
      [ "${lines[1]}" = "i=2 e=zero" ]
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "name: \${!ref} gives the target's name and plain unset acts on the target" {
  run_lab "$LAB_DIR/nameref_probe.sh" name
  case $(nameref_kind) in
    bash | ksh93 | mksh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "refers_to=target value=hit" ]
      [ "${lines[1]}" = "after_unset target=UNSET refers_to=target" ]
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "unset-n: bash and ksh93 remove only the reference, mksh has no unset -n" {
  run_lab "$LAB_DIR/nameref_probe.sh" unset-n
  case $(nameref_kind) in
    bash | ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = "unset_n_status=0 target=hit" ]
      ;;
    mksh)
      # unset is a special built-in, so its usage error ends the script
      [ "$status" -eq 1 ]
      output_has "unset: -n: unknown option"
      output_lacks "unset_n_status="
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "selfref: typeset -n x=x at global scope is refused by every nameref shell" {
  run_lab "$LAB_DIR/nameref_probe.sh" selfref
  case $(nameref_kind) in
    bash)
      [ "$status" -eq 0 ]
      output_has "x: nameref variable self references not allowed"
      output_has "selfref_status=1"
      ;;
    ksh93)
      # a failing special built-in typeset ends the script
      [ "$status" -eq 1 ]
      output_has "x: invalid self reference"
      output_lacks "selfref_status="
      ;;
    mksh)
      [ "$status" -eq 0 ]
      output_has "x: expression recurses on parameter"
      output_has "selfref_status=1"
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "circular-local: bash 5 sends a self-named nameref to the global, bash 4, ksh93 and mksh to the caller" {
  run_lab "$LAB_DIR/nameref_probe.sh" circular-local
  case $(nameref_kind) in
    bash)
      [ "$status" -eq 0 ]
      if bash_at_least 5 0; then
        # CHANGES 5.0-alpha (i): a nameref resolution loop in a function resolves at global scope
        output_has "out: circular name reference"
        output_has "caller_out=caller_local"
        output_has "global_out=assigned"
      else
        output_has "caller_out=assigned"
        output_has "global_out=global"
      fi
      ;;
    ksh93)
      # typeset -n out=$1 inside function NAME { } resolves $1 in the caller's scope
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "caller_out=assigned" ]
      [ "${lines[1]}" = "global_out=global" ]
      ;;
    mksh)
      # the typeset fails, so out=assigned is a plain dynamic-scope write to the caller's local
      [ "$status" -eq 0 ]
      output_has "out: expression recurses on parameter"
      output_has "caller_out=assigned"
      output_has "global_out=global"
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "asym: in bash a read through the nameref finds the callee's local while the write skips it" {
  run_lab "$LAB_DIR/nameref_probe.sh" asym
  case $(nameref_kind) in
    bash)
      # reads resolve the name from the innermost frame; bind_variable starts at the nameref's frame
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "read_through_ref=callee_local" ]
      [ "${lines[1]}" = "callee_target=callee_local" ]
      [ "${lines[2]}" = "holder_target=written" ]
      [ "${lines[3]}" = "global_target=written" ]
      [ "${lines[4]}" = "global_ref=UNSET" ]
      ;;
    mksh)
      # both directions resolve from the innermost frame
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "read_through_ref=callee_local" ]
      [ "${lines[1]}" = "callee_target=written" ]
      [ "${lines[2]}" = "holder_target=global" ]
      [ "${lines[3]}" = "global_target=global" ]
      [ "${lines[4]}" = "global_ref=UNSET" ]
      ;;
    ksh93)
      # static scope: the callee cannot see holder's ref at all, so it reads and creates a global ref
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "read_through_ref=" ]
      [ "${lines[1]}" = "callee_target=callee_local" ]
      [ "${lines[2]}" = "holder_target=global" ]
      [ "${lines[3]}" = "global_target=global" ]
      [ "${lines[4]}" = "global_ref=written" ]
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "for: a nameref loop variable binds each word in bash and ksh93, mksh refuses an empty nameref" {
  run_lab "$LAB_DIR/nameref_probe.sh" for
  case $(nameref_kind) in
    bash | ksh93)
      [ "$status" -eq 0 ]
      [ "$output" = "a=10 b=20 c=30" ]
      ;;
    mksh)
      [ "$status" -eq 0 ]
      output_has "v: empty nameref target"
      output_has "a=1 b=2 c=3"
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "positional: bash and ksh93 refuse typeset -n r=1, mksh binds r to \$1" {
  run_lab "$LAB_DIR/nameref_probe.sh" positional
  case $(nameref_kind) in
    bash)
      [ "$status" -eq 0 ]
      output_has "typeset_status=1 r=UNSET"
      ;;
    ksh93)
      [ "$status" -eq 1 ]
      output_has "1: invalid variable name"
      output_lacks "typeset_status="
      ;;
    mksh)
      [ "$status" -eq 0 ]
      [ "$output" = "typeset_status=0 r=first" ]
      ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "print: typeset -p shows the reference in bash and ksh93, the target in mksh" {
  run_lab "$LAB_DIR/nameref_probe.sh" print
  case $(nameref_kind) in
    bash) [ "$output" = 'declare -n r="t"' ] ;;
    ksh93) [ "$output" = "typeset -n r=t" ] ;;
    mksh) [ "$output" = "typeset t=1" ] ;;
    none) assert_unsupported ;;
    *) false ;;
  esac
}

@test "unset-caller: a callee's unset removes the caller's local in bash, mksh and yash" {
  run_lab "$LAB_DIR/scope_probe.sh" unset-caller
  case $FAMILY in
    bash | mksh | yash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "inner_after_unset=global" ]
      [ "${lines[1]}" = "outer_after_inner=global" ]
      [ "${lines[2]}" = "top=global" ]
      ;;
    dash | busybox | posh | zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "inner_after_unset=UNSET" ]
      [ "${lines[1]}" = "outer_after_inner=UNSET" ]
      [ "${lines[2]}" = "top=global" ]
      ;;
    ksh93)
      # no local built-in: there is one x, and unset removes it
      [ "$status" -eq 0 ]
      output_has "local: not found"
      output_has "inner_after_unset=UNSET"
      output_has "top=UNSET"
      ;;
    *) false ;;
  esac
}

@test "unset-own: unsetting your own local keeps it local, except in mksh and yash" {
  run_lab "$LAB_DIR/scope_probe.sh" unset-own
  case $FAMILY in
    bash | dash | busybox | posh | zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "after_unset=UNSET" ]
      [ "${lines[1]}" = "top=global" ]
      ;;
    mksh | yash)
      # the local is gone, so the global shows through and the next assignment overwrites it
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "after_unset=global" ]
      [ "${lines[1]}" = "top=again" ]
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      output_has "local: not found"
      output_has "after_unset=UNSET"
      output_has "top=again"
      ;;
    *) false ;;
  esac
}

@test "upvar: unset plus eval returns into the caller's local only where unset leaves it in place" {
  run_lab "$LAB_DIR/scope_probe.sh" upvar
  case $FAMILY in
    bash | mksh | yash)
      # unset deleted f's local result, so eval created a global
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "f_result=computed" ]
      [ "${lines[1]}" = "top_result=computed" ]
      ;;
    dash | busybox | posh | zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "f_result=computed" ]
      [ "${lines[1]}" = "top_result=UNSET" ]
      ;;
    ksh93)
      # without local, g's tmp is never set, so eval assigns the empty string
      [ "$status" -eq 0 ]
      output_has "local: not found"
      output_has "f_result="
      output_has "top_result="
      output_lacks "computed"
      ;;
    *) false ;;
  esac
}

@test "export-local: a local over an exported name is exported in bash, dash and BusyBox only" {
  run_lab "$LAB_DIR/scope_probe.sh" export-local
  case $FAMILY in
    bash | dash | busybox)
      [ "$status" -eq 0 ]
      [ "$output" = "child_sees=E=newval" ]
      ;;
    mksh | posh | yash)
      # the local is not exported, so the child still gets the global's value
      [ "$status" -eq 0 ]
      [ "$output" = "child_sees=E=exported" ]
      ;;
    zsh | zsh-sh)
      # the unexported local hides the global, so the child gets no E at all
      [ "$status" -eq 0 ]
      [ "$output" = "child_sees=ABSENT" ]
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      output_has "local: not found"
      output_has "child_sees=E=newval"
      ;;
    *) false ;;
  esac
}

@test "readonly-local: bash refuses a local over a readonly global, dash and BusyBox exit, others allow it" {
  run_lab "$LAB_DIR/scope_probe.sh" readonly-local
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      output_has "local: ro: readonly variable"
      output_has "local_status=1 inside=frozen"
      output_has "top=frozen"
      ;;
    dash | busybox)
      [ "$status" -eq 2 ]
      output_has "is read only"
      output_lacks "local_status="
      ;;
    zsh | zsh-sh | posh | mksh | yash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "local_status=0 inside=thawed" ]
      [ "${lines[1]}" = "top=frozen" ]
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      output_has "local_status=127 inside=frozen"
      ;;
    *) false ;;
  esac
}

@test "declare-g: bash -g skips the caller's local and writes the global, zsh -g writes the caller's local" {
  run_lab "$LAB_DIR/scope_probe.sh" declare-g
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      if bash_at_least 4 2; then
        [ "${lines[0]}" = "writer_sees=caller_local" ]
        [ "${lines[1]}" = "caller_sees=caller_local" ]
        [ "${lines[2]}" = "top=from_g" ]
      else
        # declare -g arrived in bash 4.2
        output_has "declare: -g: invalid option"
        output_has "top=global"
      fi
      ;;
    zsh | zsh-sh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "writer_sees=from_g" ]
      [ "${lines[1]}" = "caller_sees=from_g" ]
      [ "${lines[2]}" = "top=global" ]
      ;;
    dash | busybox | posh | mksh | yash | ksh93)
      # no declare built-in
      [ "$status" -eq 0 ]
      output_has "declare"
      output_lacks "from_g"
      ;;
    *) false ;;
  esac
}

@test "localvar-unset: bash 5.0+ option keeps a caller's local hidden after a callee's unset" {
  run_lab "$LAB_DIR/scope_probe.sh" localvar-unset
  case $FAMILY in
    bash)
      if bash_at_least 5 0; then
        [ "$status" -eq 0 ]
        [ "${lines[0]}" = "inner_after_unset=UNSET" ]
        [ "${lines[1]}" = "outer_after_inner=UNSET" ]
      else
        [ "$status" -eq 3 ]
        [ "$output" = "localvar_unset=unsupported" ]
      fi
      ;;
    dash | busybox | posh | mksh | yash | ksh93 | zsh | zsh-sh)
      [ "$status" -eq 3 ]
      [ "$output" = "localvar_unset=unsupported" ]
      ;;
    *) false ;;
  esac
}

@test "inherit: a fresh local is unset except in dash and native zsh; local -I and localvar_inherit copy the outer value" {
  run_lab "$LAB_DIR/scope_probe.sh" inherit
  case $FAMILY in
    bash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "plain=UNSET" ]
      if bash_at_least 5 1; then
        [ "${lines[1]}" = "local_I=outer_value" ]
        [ "${lines[2]}" = "plain=outer_value" ]
        [ "${#lines[@]}" -eq 3 ]
      elif bash_at_least 5 0; then
        # local -I fails, so v stays the outer variable; the shopt already works
        output_has "local: -I: invalid option"
        output_has "local_I=outer_value"
        [ "${lines[${#lines[@]} - 1]}" = "plain=outer_value" ]
      else
        output_has "local: -I: invalid option"
        output_has "local_I=outer_value"
        output_has "localvar_inherit=unsupported"
      fi
      ;;
    dash)
      # dash.1: a local "inherits the initial value and exported and readonly flags";
      # then the bad option to local ends the script
      [ "$status" -eq 2 ]
      [ "${lines[0]}" = "plain=outer_value" ]
      output_has "-I: bad variable name"
      output_lacks "local_I="
      ;;
    busybox)
      [ "$status" -eq 2 ]
      [ "${lines[0]}" = "plain=UNSET" ]
      output_has "-I: bad variable name"
      output_lacks "local_I="
      ;;
    zsh)
      # a native zsh local starts set and empty
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "plain=" ]
      output_has "bad option: -I"
      output_has "localvar_inherit=unsupported"
      ;;
    zsh-sh)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "plain=UNSET" ]
      output_has "bad option: -I"
      output_has "localvar_inherit=unsupported"
      ;;
    mksh | yash)
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "plain=UNSET" ]
      output_has "-I"
      output_has "local_I=outer_value"
      output_has "localvar_inherit=unsupported"
      ;;
    posh)
      # the failed local ends with_I before it prints
      [ "$status" -eq 0 ]
      [ "${lines[0]}" = "plain=UNSET" ]
      output_has "local: -I: not identifier"
      output_lacks "local_I="
      output_has "localvar_inherit=unsupported"
      ;;
    ksh93)
      [ "$status" -eq 0 ]
      output_has "local: not found"
      output_has "plain=outer_value"
      output_has "localvar_inherit=unsupported"
      ;;
    *) false ;;
  esac
}

@test "valsub: \${| cmd; } returns a value through a local REPLY without a fork in bash 5.3 and mksh" {
  run_lab "$LAB_DIR/scope_probe.sh" valsub
  [ "$status" -eq 0 ]
  case $FAMILY in
    bash)
      if bash_at_least 5 3; then
        # value, then n=1 (f ran in this shell), then REPLY restored to the outer value
        [ "$output" = "valsub=fn_result 1 outer" ]
      else
        [ "$output" = "valsub=unsupported" ]
      fi
      ;;
    mksh) [ "$output" = "valsub=fn_result 1 outer" ] ;;
    dash | busybox | posh | yash | ksh93 | zsh | zsh-sh) [ "$output" = "valsub=unsupported" ] ;;
    *) false ;;
  esac
}
