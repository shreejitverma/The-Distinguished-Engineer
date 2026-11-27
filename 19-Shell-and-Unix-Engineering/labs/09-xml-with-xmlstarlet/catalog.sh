#!/bin/sh

if [ "$#" -ne 3 ]; then
  printf 'Usage: %s CATALOG SCHEMA SCRATCH_PARENT\n' "$0" >&2
  exit 2
fi

catalog=$1
schema=$2
scratch=$(mktemp -d "$3/xmlstarlet.XXXXXXXX") || exit 1
trap 'rm -f "$scratch/edited.xml"; rmdir "$scratch"' 0
edited=$scratch/edited.xml
ns=c=urn:example:catalog

xmlstarlet val -q -r "$schema" "$catalog" || exit 1
pending='/c:catalog/c:item[@id="b"]/@state'
matches=$(xmlstarlet --no-doc-namespace sel -T -N "$ns" -t \
  -v 'count(/c:catalog/c:item[@id="b"][last()=1][@state="pending"])' "$catalog") || exit 1
if [ "$matches" != 1 ]; then
  printf 'Expected one pending item b, found %s\n' "$matches" >&2
  exit 1
fi

xmlstarlet --no-doc-namespace ed -N "$ns" \
  -u "$pending" -v active "$catalog" >"$edited" || exit 1
xmlstarlet val -q -r "$schema" "$edited" || exit 1
state=$(xmlstarlet --no-doc-namespace sel -T -N "$ns" -t \
  -v "string($pending)" "$edited") || exit 1
if [ "$state" != active ]; then
  printf 'Expected item b to be active, found %s\n' "$state" >&2
  exit 1
fi

xmlstarlet --no-doc-namespace sel -T -N "$ns" -t \
  -m '/c:catalog/c:item[@state="active"]' \
  -v '@id' -o ' | ' -v 'string(.)' -n "$edited"
