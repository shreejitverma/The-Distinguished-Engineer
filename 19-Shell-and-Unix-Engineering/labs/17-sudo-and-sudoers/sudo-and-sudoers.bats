#!/usr/bin/env bats
# Tests for sudo and sudoers security mechanisms, audit rules,
# argument crossing traps, environment sanitization, and UID bypass behavior.

setup() {
  load ../lib/lab
  SUDOERS_AUDIT="$BATS_TEST_DIRNAME/sudoers_audit.sh"
  ARG_CROSS_PROBE="$BATS_TEST_DIRNAME/arg_cross_probe.sh"
  ENV_FILTER="$BATS_TEST_DIRNAME/env_filter.sh"
  UID_BYPASS_SRC="$BATS_TEST_DIRNAME/uid_bypass_probe.c"
}

@test "sudoers_audit detects reserved words used as alias names" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/bad_alias.sudoers"
User_Alias ALL = root
alice ALL = (ALL) /usr/bin/id
EOF
  run_lab "$SUDOERS_AUDIT" "$BATS_TEST_TMPDIR/bad_alias.sudoers"
  [ "$status" -eq 1 ]
  case "$output" in
    *"[CRITICAL]"*"reserved word ALL used as alias name"*) ;;
    *) false ;;
  esac
}

@test "sudoers_audit detects ineffective negation with ALL" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/bad_negation.sudoers"
Defaults env_reset
Defaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
alice ALL = (ALL) ALL, !/bin/sh
EOF
  run_lab "$SUDOERS_AUDIT" "$BATS_TEST_TMPDIR/bad_negation.sudoers"
  [ "$status" -eq 1 ]
  case "$output" in
    *"[CRITICAL]"*"ineffective command subtraction"*) ;;
    *) false ;;
  esac
}

@test "sudoers_audit detects unanchored wildcard argument crossing" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/bad_wildcard.sudoers"
Defaults env_reset
Defaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
operator ALL = (root) /bin/cat /var/log/messages*
EOF
  run_lab "$SUDOERS_AUDIT" "$BATS_TEST_TMPDIR/bad_wildcard.sudoers"
  [ "$status" -eq 1 ]
  case "$output" in
    *"[CRITICAL]"*"wildcard argument crossing trap"*) ;;
    *) false ;;
  esac
}

@test "sudoers_audit detects raw editor granted without sudoedit or NOEXEC" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/bad_editor.sudoers"
Defaults env_reset
Defaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
admin ALL = (root) /usr/bin/vim
EOF
  run_lab "$SUDOERS_AUDIT" "$BATS_TEST_TMPDIR/bad_editor.sudoers"
  [ "$status" -eq 1 ]
  case "$output" in
    *"[CRITICAL]"*"editor granted without sudoedit or NOEXEC"*) ;;
    *) false ;;
  esac
}

@test "sudoers_audit passes clean hardened sudoers configuration" {
  cat <<'EOF' >"$BATS_TEST_TMPDIR/clean.sudoers"
Defaults env_reset
Defaults secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
operator ALL = (root) sudoedit /var/log/messages
EOF
  run_lab "$SUDOERS_AUDIT" "$BATS_TEST_TMPDIR/clean.sudoers"
  [ "$status" -eq 0 ]
  case "$output" in
    *"Audit complete: 0 critical, 0 warnings"*) ;;
    *) false ;;
  esac
}

@test "arg_cross_probe proves wildcard matches cross argument boundary" {
  run_lab "$ARG_CROSS_PROBE" glob '/var/log/messages*' '/var/log/messages' '/etc/shadow'
  [ "$status" -eq 0 ]
  [ "$output" = 'MATCHED: concatenated "/var/log/messages /etc/shadow" matched glob "/var/log/messages*"' ]
}

@test "arg_cross_probe proves regex pattern rejects argument crossing" {
  run_lab "$ARG_CROSS_PROBE" regex '^/var/log/messages/[a-zA-Z0-9._-]+$' '/var/log/messages' '/etc/shadow'
  [ "$status" -eq 1 ]
  [ "$output" = 'DENIED: concatenated "/var/log/messages /etc/shadow" did not match regex "^/var/log/messages/[a-zA-Z0-9._-]+$"' ]
}

@test "arg_cross_probe accepts the one intended argument under the anchored regex" {
  run_lab "$ARG_CROSS_PROBE" regex '^/var/log/messages/[a-zA-Z0-9._-]+$' '/var/log/messages/today.log'
  [ "$status" -eq 0 ]
  [ "$output" = 'MATCHED: concatenated "/var/log/messages/today.log" matched regex "^/var/log/messages/[a-zA-Z0-9._-]+$"' ]
}

@test "arg_cross_probe shows a regex without anchors still matches across arguments" {
  run_lab "$ARG_CROSS_PROBE" regex '/var/log/messages' '/var/log/messages' '/etc/shadow'
  [ "$status" -eq 0 ]
  [ "$output" = 'MATCHED: concatenated "/var/log/messages /etc/shadow" matched regex "/var/log/messages"' ]
}

@test "arg_cross_probe shows the closing anchor is what rejects the second argument" {
  run_lab "$ARG_CROSS_PROBE" regex '^/var/log/messages[^[:space:]]*' '/var/log/messages' '/etc/shadow'
  [ "$status" -eq 0 ]
  [ "$output" = 'MATCHED: concatenated "/var/log/messages /etc/shadow" matched regex "^/var/log/messages[^[:space:]]*"' ]
  run_lab "$ARG_CROSS_PROBE" regex '^/var/log/messages[^[:space:]]*$' '/var/log/messages' '/etc/shadow'
  [ "$status" -eq 1 ]
  [ "$output" = 'DENIED: concatenated "/var/log/messages /etc/shadow" did not match regex "^/var/log/messages[^[:space:]]*$"' ]
}

@test "env_filter strips LD_PRELOAD, BASH_ENV, IFS, and shell functions" {
  run_lab "$ENV_FILTER" "LD_PRELOAD=/tmp/evil.so" "BASH_ENV=/tmp/rc" "IFS=:" "FOO=() { :; };" "USER=alice" "PATH=/tmp/evil:/bin"
  [ "$status" -eq 0 ]
  case "$output" in
    *"STRIPPED: LD_PRELOAD BASH_ENV IFS FOO"*) ;;
    *) false ;;
  esac
  case "$output" in
    *"RETAINED:"*"USER=alice"*) ;;
    *) false ;;
  esac
  case "$output" in
    *"RETAINED:"*"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"*) ;;
    *) false ;;
  esac
}

@test "uid_bypass_probe proves setreuid(-1, -1) leaves credentials unchanged" {
  probe_bin="$BATS_TEST_TMPDIR/uid_bypass_probe"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -D_FORTIFY_SOURCE=2 "$UID_BYPASS_SRC" -o "$probe_bin"
  run "$probe_bin"
  [ "$status" -eq 0 ]
  case "$output" in
    *"SUCCESS: setreuid(-1, -1) left IDs unchanged"*) ;;
    *) false ;;
  esac
}

@test "visudo validates syntax and ignores dotfiles and backup files in includedir" {
  if ! command -v visudo >/dev/null 2>&1; then
    run_lab_c 'echo "visudo not installed"'
    [ "$status" -eq 0 ]
  else
    inc_dir="$BATS_TEST_TMPDIR/sudoers.d"
    mkdir -p "$inc_dir"
    printf 'alice ALL = (ALL) /bin/ls\n' >"$inc_dir/01_clean"
    # Files ending in ~ or containing . must be ignored by sudoers
    printf 'SYNTAX ERROR HERE\n' >"$inc_dir/99_corrupt~"
    printf 'SYNTAX ERROR HERE\n' >"$inc_dir/50_corrupt.bak"

    main_conf="$BATS_TEST_TMPDIR/main.sudoers"
    printf '@includedir %s\n' "$inc_dir" >"$main_conf"

    run visudo -c -s -f "$main_conf"
    [ "$status" -eq 0 ]
    case "$output" in
      *"01_clean: parsed OK"*) ;;
      *) false ;;
    esac
  fi
}
