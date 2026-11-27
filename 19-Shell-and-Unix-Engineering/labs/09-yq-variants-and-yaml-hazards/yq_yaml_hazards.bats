#!/usr/bin/env bats

setup() {
  load ../lib/lab
  DETECT="$BATS_TEST_DIRNAME/detect-yq.sh"
  SAFE_UPDATE="$BATS_TEST_DIRNAME/safe-yaml-update.sh"
}

@test "detect-yq identifies the installed yq variant" {
  run_lab "$DETECT" --variant
  [ "$status" -eq 0 ]
  [ "$output" = "mikefarah" ]

  run_lab "$DETECT"
  [ "$status" -eq 0 ]
  case "$output" in
    *"variant=mikefarah"*)
      true
      ;;
    *)
      false
      ;;
  esac
}

@test "raw yq in-place edit severs a symbolic link" {
  target="$BATS_TEST_TMPDIR/target.yaml"
  link="$BATS_TEST_TMPDIR/link.yaml"
  printf 'service:\n  name: original\n' >"$target"
  ln -s "$target" "$link"

  # Confirm link is a symlink before update
  [ -L "$link" ]

  # Raw yq in-place edit uses rename(2) onto the link path
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq -i ".service.name = \"mutated\"" "$1"' test "$link"
  [ "$status" -eq 0 ]

  # The symlink has been severed and replaced with a regular file
  [ ! -L "$link" ]
  [ -f "$link" ]

  # The real target file remained untouched
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval ".service.name" "$1"' read "$target"
  [ "$status" -eq 0 ]
  [ "$output" = "original" ]

  # The severed link file holds the mutated value
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval ".service.name" "$1"' read "$link"
  [ "$status" -eq 0 ]
  [ "$output" = "mutated" ]
}

@test "safe-yaml-update preserves symbolic link and mutates real target" {
  target="$BATS_TEST_TMPDIR/safe_target.yaml"
  link="$BATS_TEST_TMPDIR/safe_link.yaml"
  printf 'service:\n  name: original\n' >"$target"
  ln -s "$target" "$link"

  run_lab "$SAFE_UPDATE" "$link" ".service.name" "updated_safely"
  [ "$status" -eq 0 ]

  # The symlink remains intact
  [ -L "$link" ]

  # The real target was updated
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval ".service.name" "$1"' read "$target"
  [ "$status" -eq 0 ]
  [ "$output" = "updated_safely" ]
}

@test "safe-yaml-update forces string tag on Norway country code" {
  conf="$BATS_TEST_TMPDIR/norway.yaml"
  printf 'cluster:\n  region: eu-north\n' >"$conf"

  # Pass country code NO
  run_lab "$SAFE_UPDATE" "$conf" ".cluster.country" "NO"
  [ "$status" -eq 0 ]

  # Tag must be !!str
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval ".cluster.country | tag" "$1"' read "$conf"
  [ "$status" -eq 0 ]
  [ "$output" = "!!str" ]

  # JSON serialization must produce string "NO", not boolean false
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq -o=json eval ".cluster.country" "$1"' read "$conf"
  [ "$status" -eq 0 ]
  [ "$output" = '"NO"' ]
}

@test "safe-yaml-update forces string tag on octal permission code" {
  conf="$BATS_TEST_TMPDIR/perms.yaml"
  printf 'file:\n  path: /etc/app.conf\n  raw_mode: 0644\n' >"$conf"

  # Raw unquoted 0644 is resolved by mikefarah/yq YAML 1.2 JSON encoder as decimal 644
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq -o=json eval ".file.raw_mode" "$1"' read "$conf"
  [ "$status" -eq 0 ]
  [ "$output" = "644" ]

  run_lab "$SAFE_UPDATE" "$conf" ".file.mode" "0644"
  [ "$status" -eq 0 ]

  # Tag must be !!str
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval ".file.mode | tag" "$1"' read "$conf"
  [ "$status" -eq 0 ]
  [ "$output" = "!!str" ]

  # JSON serialization must preserve string "0644"
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq -o=json eval ".file.mode" "$1"' read "$conf"
  [ "$status" -eq 0 ]
  [ "$output" = '"0644"' ]

  # Explicit --as-string forces string tag on arbitrary scalar values
  run_lab "$SAFE_UPDATE" --as-string "$conf" ".file.version" "1.10"
  [ "$status" -eq 0 ]
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq -o=json eval ".file.version" "$1"' read "$conf"
  [ "$status" -eq 0 ]
  [ "$output" = '"1.10"' ]
}

@test "unexploded alias mutation corrupts shared base anchor" {
  conf="$BATS_TEST_TMPDIR/anchor_leak.yaml"
  cat <<'EOF' >"$conf"
defaults: &base
  timeout: 5000
development:
  <<: *base
  database: dev_db
production:
  <<: *base
  database: prod_db
EOF

  # Naive update mutates the underlying node referenced by the alias pointer
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval -i ".development.timeout = 1000" "$1"' update "$conf"
  [ "$status" -eq 0 ]

  # Production timeout was unintentionally mutated to 1000
  # Silence stderr to suppress yq merge-anchor warning
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval ".production.timeout" "$1" 2>/dev/null' read "$conf"
  [ "$status" -eq 0 ]
  [ "$output" = "1000" ]
}

@test "safe-yaml-update with isolate-anchors prevents cross-environment corruption" {
  conf="$BATS_TEST_TMPDIR/anchor_isolated.yaml"
  cat <<'EOF' >"$conf"
defaults: &base
  timeout: 5000
development:
  <<: *base
  database: dev_db
production:
  <<: *base
  database: prod_db
EOF

  run_lab "$SAFE_UPDATE" --isolate-anchors "$conf" ".development.timeout" "1000"
  [ "$status" -eq 0 ]

  # Development has 1000
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval ".development.timeout" "$1"' read "$conf"
  [ "$status" -eq 0 ]
  [ "$output" = "1000" ]

  # Production remains untouched at 5000
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval ".production.timeout" "$1"' read "$conf"
  [ "$status" -eq 0 ]
  [ "$output" = "5000" ]
}

@test "tab character in block indentation triggers parser error and exit status 1" {
  bad_yaml="$BATS_TEST_TMPDIR/tabs.yaml"
  printf 'root:\n\tchild: bad\n' >"$bad_yaml"

  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq eval "." "$1" 2>&1' parse "$bad_yaml"
  [ "$status" -eq 1 ]
  case "$output" in
    *"cannot start any token"* | *"found character that cannot start"* | *"tab"*)
      true
      ;;
    *)
      false
      ;;
  esac
}

@test "exit status flag -e returns 1 on missing key and 0 on present key" {
  doc="$BATS_TEST_TMPDIR/exit_test.yaml"
  printf 'existing_key: present_value\n' >"$doc"

  # Existing key with -e succeeds
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq -e ".existing_key" "$1" >/dev/null' test "$doc"
  [ "$status" -eq 0 ]

  # Missing key with -e returns exit status 1
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq -e ".nonexistent_key" "$1" >/dev/null 2>&1' test "$doc"
  [ "$status" -eq 1 ]

  # Missing key without -e returns exit status 0 (silent null)
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq ".nonexistent_key" "$1" >/dev/null' test "$doc"
  [ "$status" -eq 0 ]
}

@test "security flag security-disable-file-ops blocks load operator" {
  # shellcheck disable=SC2016 # literal expression for yq
  run_lab_c 'yq --security-disable-file-ops -n "load(\"/etc/hosts\")" 2>&1' test
  [ "$status" -eq 1 ]
  case "$output" in
    *"file operations have been disabled"*)
      true
      ;;
    *)
      false
      ;;
  esac
}

@test "shell under test belongs to a recognized shell family" {
  case $(lab_shell_family) in
    bash | zsh | zsh-sh | yash | busybox | posh | mksh | ksh93 | dash)
      true
      ;;
    *)
      false
      ;;
  esac
}
