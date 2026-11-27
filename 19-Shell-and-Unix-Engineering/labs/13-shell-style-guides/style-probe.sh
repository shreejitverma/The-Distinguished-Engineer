#!/bin/sh
# style-probe.sh: probe script demonstrating mechanisms enforced by shell style guides.
# Usage: style-probe.sh MODE [ARG...]
# Modes:
#   func_syntax       - checks POSIX vs Korn vs hybrid function definition syntax
#   mask_return       - compares local/export declaration masking vs separate assignment
#   scope_leak        - tests dynamic vs lexical variable scoping in shell functions
#   case_collision    - tests collision with system uppercase variables (PATH, UID)
#   bracket_test      - exit status of [ -n $empty ] vs [[ -n $empty ]] with an unquoted empty variable
#   main_guard        - tests execution safety under script truncation with main "$@"
#   array_quote       - compares quoted array/parameter expansion against unquoted splitting
#   pipe_while        - whether a variable set inside "producer | while read" survives the loop
#   arith_zero        - exit status of the standalone arithmetic command (( i++ )) when it evaluates to 0

set -u

mode=${1:?usage: style-probe.sh MODE}
shift

case "$mode" in
  func_syntax)
    # POSIX: fname() { ... }
    # Korn:  function fname { ... }
    # Hybrid: function fname() { ... }
    posix_res=$(eval 'f_posix() { printf ok; }; f_posix' 2>/dev/null) || posix_res="error"
    korn_res=$(eval 'function f_korn { printf ok; }; f_korn' 2>/dev/null) || korn_res="error"
    hybrid_res=$(eval 'function f_hybrid() { printf ok; }; f_hybrid' 2>/dev/null) || hybrid_res="error"
    printf 'posix=%s korn=%s hybrid=%s\n' "$posix_res" "$korn_res" "$hybrid_res"
    ;;

  mask_return)
    # Test 1: masked local declaration
    masked_status=$(
      f_masked() {
        # shellcheck disable=SC2046,SC2155,SC3043 # SC2155 return code masking is the behavior under test
        local val=$(exit 42) 2>/dev/null
        return $?
      }
      f_masked 2>/dev/null
      printf '%d' "$?"
    )

    # Test 2: separate declaration and assignment
    separate_status=$(
      f_separate() {
        # shellcheck disable=SC3043 # local declaration is under test
        local val 2>/dev/null || true
        # shellcheck disable=SC2034,SC2312 # intentional assignment to capture status
        val=$(exit 42)
        return $?
      }
      f_separate 2>/dev/null
      printf '%d' "$?"
    )

    # Test 3: export declaration masking
    export_status=$(
      (
        # shellcheck disable=SC2046,SC2155 # SC2155 return code masking is the behavior under test
        export exp_val=$(exit 42) 2>/dev/null
        printf '%d' "$?"
      )
    )

    # Test 4: readonly declaration masking
    readonly_status=$(
      (
        # shellcheck disable=SC2034,SC2046,SC2155 # SC2155 return code masking is the behavior under test
        readonly ro_val=$(exit 42) 2>/dev/null
        printf '%d' "$?"
      )
    )

    printf 'masked=%s separate=%s export=%s readonly=%s\n' \
      "$masked_status" "$separate_status" "$export_status" "$readonly_status"
    ;;

  scope_leak)
    # Tests dynamic scoping: caller's local variable visible to and mutated by callee
    inner_var_read="unset"
    outer_var_after="unset"

    inner_func() {
      inner_var_read=${scoped_var:-unset}
      scoped_var="mutated_by_inner"
    }

    outer_func() {
      # shellcheck disable=SC3043 # local scope is under test
      local scoped_var="initial_outer" 2>/dev/null || scoped_var="initial_outer"
      inner_func
      outer_var_after=$scoped_var
    }

    scoped_var="global_val"
    outer_func
    printf 'inner_read=%s outer_after=%s global=%s\n' \
      "$inner_var_read" "$outer_var_after" "$scoped_var"
    ;;

  case_collision)
    # Tests collision with special shell variables (UID) and PATH clobbering
    # 1. Check if UID is readonly in this shell. The assignment runs in a subshell because a
    #    readonly assignment error aborts a non-interactive shell in POSIX mode (XCU 2.8.1).
    uid_mod_status=0
    (eval 'UID=99999') 2>/dev/null || uid_mod_status=$?

    # 2. Check that lowercase application variable does not collide
    user_id="app_1234"

    # 3. Check PATH clobbering: assigning empty PATH breaks external command lookup
    orig_path=$PATH
    # shellcheck disable=SC2123 # PATH clobbering collision is under test
    PATH="/nonexistent/empty/dir"
    cmd_status=0
    # Attempt to run true as an external utility through env
    /usr/bin/env true 2>/dev/null || cmd_status=$?
    PATH=$orig_path

    printf 'uid_status=%d lowercase=%s path_clobber_status=%d\n' \
      "$uid_mod_status" "$user_id" "$cmd_status"
    ;;

  bracket_test)
    # POSIX test with one operand is true when the operand is non-empty (XCU test, 1 argument),
    # so an unquoted empty expansion that vanishes leaves the lone operand "-n", which is non-empty.
    # The keyword [[ ... ]] does no field splitting, so the empty operand stays an operand.
    empty=""

    # shellcheck disable=SC2070,SC2086,SC2248,SC2319 # unquoted empty operand and status capture under test
    single_unquoted=$(
      [ -n $empty ]
      echo $?
    )

    # shellcheck disable=SC2319 # condition status capture
    single_quoted=$(
      [ -n "$empty" ]
      echo $?
    )

    # eval keeps shells without the keyword from failing the whole script at parse time
    # shellcheck disable=SC2319 # condition status capture
    double_unquoted=$(
      eval '[[ -n $empty ]]' 2>/dev/null
      echo $?
    )

    printf 'single_unquoted=%s single_quoted=%s double_unquoted=%s\n' \
      "$single_unquoted" "$single_quoted" "$double_unquoted"
    ;;

  main_guard)
    # Tests execution safety of main "$@" encapsulation under simulated script truncation.
    # $1: mode: "linear" or "guarded"
    # $2: target file
    guard_type=${1:-guarded}
    tmp_target=${2:-}

    if [ -z "$tmp_target" ]; then
      printf 'error: missing target file\n' >&2
      exit 1
    fi

    if [ "$guard_type" = "linear" ]; then
      # A linear script executes line by line immediately.
      # When truncated after step1, step1 has already executed!
      {
        printf 'printf "step1\\n"\n'
        # Truncation point: second step missing
      } >"$tmp_target"
    else
      # An encapsulated script puts logic in functions and calls main "$@" last.
      # When truncated before main "$@", functions are parsed into symbol tables
      # but NOTHING executes.
      {
        printf 'step1() { printf "step1\\n"; }\n'
        printf 'step2() { printf "step2\\n"; }\n'
        printf 'main() {\n  step1\n  step2\n}\n'
        # Truncation point: main "$@" omitted
      } >"$tmp_target"
    fi
    chmod +x "$tmp_target"
    ;;

  array_quote)
    # Demonstrates argument preservation with quoting vs splitting
    count_args() {
      printf '%d' "$#"
    }

    arg1="first arg with spaces"
    arg2="second*arg"

    # Set positional parameters
    set -- "$arg1" "$arg2"

    # Quoted "$@" preserves exactly 2 arguments
    quoted_count=$(count_args "$@")

    # Unquoted $* undergoes field splitting (and globbing if matching files exist)
    # shellcheck disable=SC2048,SC2086 # unquoted expansion is the behavior under test
    unquoted_count=$(count_args $*)

    printf 'quoted=%s unquoted=%s\n' "$quoted_count" "$unquoted_count"
    ;;

  pipe_while)
    # Google guide "Pipes to While": the loop is a pipeline element, so shells that run it in a
    # subshell lose the assignment; ksh93 and zsh run the last element in the current shell.
    last_line=NULL
    # shellcheck disable=SC2030,SC2031 # the lost assignment is the behavior under test
    printf 'a\nb\n' | while read -r line; do
      last_line=$line
    done
    # shellcheck disable=SC2031 # reads the value the pipeline may have lost
    printf 'last_line=%s\n' "$last_line"
    ;;

  arith_zero)
    # Google guide "Arithmetic": a standalone (( expr )) returns status 1 when expr evaluates to 0,
    # so (( i++ )) with i=0 fails under set -e. eval keeps shells without the syntax parseable.
    arith_status=$(
      eval 'i=0; (( i++ ))' 2>/dev/null
      echo $?
    )
    printf 'postinc_status=%s\n' "$arith_status"
    ;;

  *)
    printf 'error: unknown mode %s\n' "$mode" >&2
    exit 1
    ;;
esac
