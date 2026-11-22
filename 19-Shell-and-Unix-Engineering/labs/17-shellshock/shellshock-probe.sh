#!/bin/sh
# shellshock-probe.sh: audit shell function export, import, and environment evaluation boundaries.
#
# Usage:
#   shellshock-probe.sh MODE [SHELL [ARG...]]
#
# SHELL is the shell under test (default: sh); every mode starts it as a child with a crafted
# environment, so the answer describes that shell, not the shell that runs this script.
#
#   export-f            does `export -f` put BASH_FUNC_name%% in the environment of a child
#   legacy-import       does a `() {` value in an unprefixed variable run trailing code
#   legacy-name         is a function defined in an unprefixed variable (name=() { ...; }) imported
#   modern-import       is BASH_FUNC_name%%=() { ...; } imported as a function
#   trailing-rejection  is code appended after a BASH_FUNC_name%% definition refused
#   privmode-drop       does -p drop the import that works without -p
#   parser-bypass       do the CVE-2014-7169 and CVE-2014-6278 payloads still misbehave
#   name-passthrough    does the shell pass an invalid name such as BASH_FUNC_name%% to children
#   cgi-header          is an RFC 3875 header-derived variable kept as pure data
set -u

mode=${1:-help}
if [ "$#" -gt 0 ]; then
  shift
fi
if [ "$#" -eq 0 ]; then
  set -- sh
fi

case $mode in
  export-f)
    # The child shell defines and exports a function, then lists its own environment.
    out=$(
      "$@" -c '
        lab_test_fn() { printf "active\n"; }
        export -f lab_test_fn 2>/dev/null
        env
      ' 2>/dev/null
    )
    case $out in
      *'BASH_FUNC_lab_test_fn%%='*)
        printf 'export-f:supported\n'
        ;;
      *)
        printf 'export-f:unsupported\n'
        ;;
    esac
    ;;

  legacy-import)
    # In unpatched Bash <= 4.3 patch 24 this printed INJECTED before COMPLETED.
    raw_out=$(
      env x='() { :;}; printf "INJECTED\n"' "$@" -c 'printf "COMPLETED\n"' 2>&1
    )
    case $raw_out in
      *INJECTED*)
        printf 'legacy-import:vulnerable\n'
        ;;
      *COMPLETED*)
        printf 'legacy-import:safe\n'
        ;;
      *)
        printf 'legacy-import:error\n'
        ;;
    esac
    ;;

  legacy-name)
    # Unpatched Bash imported any variable whose value began with "() {" as a function.
    res=$(
      env 'lab_fn=() { printf "func-ok\n"; }' "$@" -c 'lab_fn' 2>/dev/null
    )
    case $res in
      func-ok)
        printf 'legacy-name:imported\n'
        ;;
      *)
        printf 'legacy-name:ignored\n'
        ;;
    esac
    ;;

  modern-import)
    res=$(
      env 'BASH_FUNC_lab_fn%%=() { printf "func-ok\n"; }' "$@" -c 'lab_fn' 2>/dev/null
    )
    case $res in
      func-ok)
        printf 'modern-import:imported\n'
        ;;
      *)
        printf 'modern-import:ignored\n'
        ;;
    esac
    ;;

  trailing-rejection)
    # Bash 4.3 patch 25 and later refuse the whole value when anything follows the definition.
    out=$(
      env 'BASH_FUNC_lab_fn%%=() { :; }; printf "INJECTED\n"' "$@" -c ':' 2>&1
    )
    case $out in
      *INJECTED*)
        printf 'trailing-rejection:vulnerable\n'
        ;;
      *'error importing function definition'*)
        printf 'trailing-rejection:rejected\n'
        ;;
      *)
        printf 'trailing-rejection:ignored\n'
        ;;
    esac
    ;;

  privmode-drop)
    # The control run without -p must import the function, or the -p run proves nothing.
    plain=$(
      env 'BASH_FUNC_lab_fn%%=() { printf "func-ok\n"; }' "$@" -c 'lab_fn' 2>/dev/null
    )
    priv=$(
      env 'BASH_FUNC_lab_fn%%=() { printf "func-ok\n"; }' "$@" -p -c 'lab_fn' 2>/dev/null
    )
    case $plain:$priv in
      func-ok:func-ok)
        printf 'privmode:imported\n'
        ;;
      func-ok:*)
        printf 'privmode:dropped\n'
        ;;
      *)
        printf 'privmode:no-import\n'
        ;;
    esac
    ;;

  parser-bypass)
    # Run the CVE-2014-7169 and CVE-2014-6278 payloads both prefixed (reaching the parser in a
    # patched Bash) and unprefixed (the pre-patch shape), in a scratch directory.
    # The 6278 payload prints RAN42 from printf RAN%s 42: the diagnostic that echoes the rejected
    # source line contains the text "RAN%s 42" but never "RAN42", so only execution matches.
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/shellshock-probe.XXXXXX") || exit 1
    out7169=clean
    out6278=clean
    for name in 'BASH_FUNC_x%%' X; do
      (cd "$tmp" && env "$name=() { (a)=>\\" "$@" -c 'echo date' >/dev/null 2>&1)
      if [ -e "$tmp/echo" ]; then
        out7169=file-created
      fi
      leak=$(
        cd "$tmp" && env "$name=() { _; } >_[\$(\$())] { printf RAN%s 42; }" "$@" -c ':' 2>&1
      )
      case $leak in
        *RAN42*)
          out6278=code-ran
          ;;
        *)
          :
          ;;
      esac
    done
    rm -f "$tmp/echo"
    rmdir "$tmp"
    printf 'parser-bypass:7169=%s 6278=%s\n' "$out7169" "$out6278"
    ;;

  name-passthrough)
    # Names that are not valid shell identifiers are handled differently by each shell.
    out=$(
      env 'BASH_FUNC_lab_fn%%=() { :; }' "$@" -c 'env' 2>/dev/null
    )
    case $out in
      *'BASH_FUNC_lab_fn%%='*)
        printf 'name-passthrough:passed\n'
        ;;
      *)
        printf 'name-passthrough:dropped\n'
        ;;
    esac
    ;;

  cgi-header)
    # RFC 3875 maps the header User-Agent to HTTP_USER_AGENT, which no shell treats as a function.
    payload='() { :;}; printf "INJECTED\n"'
    val=$(
      # shellcheck disable=SC2016 # expansion belongs to the child shell
      env "HTTP_USER_AGENT=$payload" "$@" -c 'printf "%s" "$HTTP_USER_AGENT"' 2>/dev/null
    )
    printf 'cgi-header:data=%s\n' "$val"
    ;;

  help | *)
    printf 'usage: shellshock-probe.sh {export-f|legacy-import|legacy-name|modern-import|trailing-rejection|privmode-drop|parser-bypass|name-passthrough|cgi-header} [SHELL [ARG...]]\n' >&2
    exit 2
    ;;
esac
