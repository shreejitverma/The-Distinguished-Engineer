#!/usr/bin/env bats

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/shellshock-probe.sh"
}

# probe MODE: run the probe under the shell under test, telling it to start that same shell as the child.
probe() {
  lab_shell_words
  run_lab "$PROBE" "$1" "${LAB_SHELL_WORDS[@]}"
}

@test "export-f puts BASH_FUNC_name%% in the environment only under bash" {
  probe export-f
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "$output" = "export-f:supported" ]
      ;;
    dash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93)
      [ "$output" = "export-f:unsupported" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "legacy value syntax in an unprefixed variable does not execute injected commands" {
  probe legacy-import
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93)
      [ "$output" = "legacy-import:safe" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "an unprefixed variable holding a function definition is not imported" {
  probe legacy-name
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93)
      [ "$output" = "legacy-name:ignored" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "modern BASH_FUNC_%% imports functions under bash but is ignored by POSIX shells" {
  probe modern-import
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "$output" = "modern-import:imported" ]
      ;;
    dash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93)
      [ "$output" = "modern-import:ignored" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "trailing commands after BASH_FUNC_%% definitions are rejected without execution" {
  probe trailing-rejection
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "$output" = "trailing-rejection:rejected" ]
      ;;
    dash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93)
      [ "$output" = "trailing-rejection:ignored" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "privileged mode (-p) drops the function import that works without -p" {
  probe privmode-drop
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      [ "$output" = "privmode:dropped" ]
      ;;
    dash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93)
      [ "$output" = "privmode:no-import" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "the CVE-2014-7169 and CVE-2014-6278 payloads do nothing in the shell under test" {
  probe parser-bypass
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93)
      [ "$output" = "parser-bypass:7169=clean 6278=clean" ]
      ;;
    *)
      false
      ;;
  esac
}

@test "an invalid environment name such as BASH_FUNC_name%% is passed on or dropped per shell" {
  probe name-passthrough
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | ksh93)
      [ "$output" = "name-passthrough:passed" ]
      ;;
    dash | posh | mksh)
      [ "$output" = "name-passthrough:dropped" ]
      ;;
    busybox)
      # Alpine's BusyBox keeps invalid names and Debian's build drops them (see the Portability
      # table), so the control is how this build treats a plainly invalid name.
      lab_shell_words
      control=$(env 'bad-ident=x' "${LAB_SHELL_WORDS[@]}" -c env)
      case $control in
        *bad-ident=x*)
          [ "$output" = "name-passthrough:passed" ]
          ;;
        *)
          [ "$output" = "name-passthrough:dropped" ]
          ;;
      esac
      ;;
    *)
      false
      ;;
  esac
}

@test "RFC 3875 CGI HTTP header mapping preserves injected strings as literal data" {
  probe cgi-header
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash | dash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93)
      [ "$output" = 'cgi-header:data=() { :;}; printf "INJECTED\n"' ]
      ;;
    *)
      false
      ;;
  esac
}
