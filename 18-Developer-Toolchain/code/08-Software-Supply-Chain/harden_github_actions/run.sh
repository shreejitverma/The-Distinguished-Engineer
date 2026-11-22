#!/usr/bin/env bash
# Lab: audit and harden a GitHub Actions release pipeline, offline, with python3 only.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export LC_ALL=C PYTHONDONTWRITEBYTECODE=1

cp -R "$HERE/fixtures" "$WORK/fixtures"
cd "$WORK"

echo "### 1. parser contract"
python3 - "$HERE" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from audit_workflows import parse_yaml, YamlError
doc = parse_yaml("""
on: {push: {tags: ["v*"]}}
permissions: {}
jobs:
  a:
    steps:
    - uses: x/y@abc # trailing comment
    - run: |
        echo "a: b # not a comment"
        echo done
      env:
        K: 'it''s'
""")
print(json.dumps(doc, sort_keys=True))
for bad in ("a: &anchor 1\n", "a:\n\tb: 1\n", "a: 1\na: 2\n"):
    try:
        parse_yaml(bad)
        print("ACCEPTED", repr(bad))
    except YamlError as exc:
        print("rejected:", str(exc)[:60])
PY

echo
echo "### 2. audit the vulnerable fixtures"
python3 "$HERE/audit_workflows.py" --root "$WORK" fixtures/before
echo "exit status: $?"

echo
echo "### 3. pin every action from the offline map"
python3 "$HERE/pin_actions.py" fixtures/pins.json fixtures/before/release.yml fixtures/before/pr-triage.yml
echo "exit status: $?"
grep -h 'uses:' fixtures/before/release.yml | sed 's/^ *- *//' | head -3

echo
echo "### 4. re-audit pinning only: GHA002 count after pinning"
python3 "$HERE/audit_workflows.py" --root "$WORK" fixtures/before | grep -c 'GHA002' || true

echo
echo "### 5. audit the hardened fixtures"
python3 "$HERE/audit_workflows.py" --root "$WORK" fixtures/after
echo "exit status: $?"

echo
echo "### 6. negative controls on the hardened release workflow"
python3 "$HERE/mutate_check.py" fixtures/after/release.yml
echo "exit status: $?"
