#!/usr/bin/env bats

setup() {
  load ../lib/lab
  CATALOG="$BATS_TEST_DIRNAME/catalog.xml"
  SCHEMA="$BATS_TEST_DIRNAME/catalog.rng"
  SCRIPT="$BATS_TEST_DIRNAME/catalog.sh"
  NS=c=urn:example:catalog
}

@test "validate and update a catalog with a path containing spaces and glob characters" {
  path="$BATS_TEST_TMPDIR/catalog [copy].xml"
  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c 'cat < "$1" > "$2"' copy "$CATALOG" "$path"
  [ "$status" -eq 0 ]

  run_lab "$SCRIPT" "$path" "$SCHEMA" "$BATS_TEST_TMPDIR"
  [ "$status" -eq 0 ]
  [ "$output" = 'a | Salt & Pepper
b | Tea' ]
  printf '%s\n' "$output"

  run_lab_c 'xmlstarlet --version'
  [ "$status" -eq 0 ]
  printf '%s\n' "$output"
}

@test "an unprefixed element path counts zero in a default namespace" {
  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c 'xmlstarlet sel -T -t -v "count(/catalog/item)" "$1"' select "$CATALOG"
  [ "$status" -eq 0 ]
  [ "$output" = 0 ]
}

@test "an empty match loop is different from a rendered scalar zero" {
  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c '
    xmlstarlet --no-doc-namespace sel -T -N "$1" -t \
      -m "/c:catalog/c:missing" -v "@id" -n "$2"
  ' select "$NS" "$CATALOG"
  [ "$status" -eq 1 ]
  [ -z "$output" ]
}

@test "sel -v emits every node but string() converts the first node" {
  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c '
    xmlstarlet --no-doc-namespace sel -T -N "$1" -t \
      -v "/c:catalog/c:item/@id" -n \
      -v "string(/c:catalog/c:item/@id)" -n "$2"
  ' select "$NS" "$CATALOG"
  [ "$status" -eq 0 ]
  [ "$output" = 'a
b
a' ]
}

@test "well-formed XML can violate the selected schema" {
  invalid="$BATS_TEST_TMPDIR/invalid.xml"
  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c '
    printf "%s\n" "<catalog xmlns=\"urn:example:catalog\"/>" > "$1" || exit 1
    xmlstarlet val -q -w "$1"
  ' validate "$invalid"
  [ "$status" -eq 0 ]
  [ -z "$output" ]

  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c 'xmlstarlet val -q -r "$1" "$2"' validate "$SCHEMA" "$invalid"
  [ "$status" -eq 1 ]
  [ -z "$output" ]
}

@test "a schema-valid catalog with no pending item b fails the edit precondition" {
  missing="$BATS_TEST_TMPDIR/missing.xml"
  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c '
    xmlstarlet --no-doc-namespace ed -N "$1" \
      -d "/c:catalog/c:item[@id=\"b\"]" "$2" > "$3"
  ' edit "$NS" "$CATALOG" "$missing"
  [ "$status" -eq 0 ]

  run_lab "$SCRIPT" "$missing" "$SCHEMA" "$BATS_TEST_TMPDIR"
  [ "$status" -eq 1 ]
  [ "$output" = 'Expected one pending item b, found 0' ]
}

@test "a pending item b with a duplicate active id also fails the edit precondition" {
  duplicate="$BATS_TEST_TMPDIR/duplicate.xml"
  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c '
    printf "%s\n" "<catalog xmlns=\"urn:example:catalog\">" \
      "<item id=\"b\" state=\"pending\">Tea</item>" \
      "<item id=\"b\" state=\"active\">Coffee</item>" "</catalog>" > "$1"
  ' fixture "$duplicate"
  [ "$status" -eq 0 ]

  run_lab "$SCRIPT" "$duplicate" "$SCHEMA" "$BATS_TEST_TMPDIR"
  [ "$status" -eq 1 ]
  [ "$output" = 'Expected one pending item b, found 0' ]
}

@test "canonicalization sorts attributes expands empty elements and omits comments" {
  # shellcheck disable=SC2016 # these expansions belong to the shell under test
  run_lab_c '
    printf "%s" "<r b=\"2\" a=\"1\"><!--note--><e/></r>" > "$1" || exit 1
    xmlstarlet c14n --without-comments "$1"
  ' canonicalize "$BATS_TEST_TMPDIR/canonical.xml"
  [ "$status" -eq 0 ]
  [ "$output" = '<r a="1" b="2"><e></e></r>' ]
}
