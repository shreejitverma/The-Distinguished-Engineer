#!/usr/bin/env bash
# requires: macos
# Lab: worktree isolation, Seatbelt confinement, and an attested ship gate for coding agents.
# Everything runs in a temp dir; paths are resolved (pwd -P) because Seatbelt matches resolved paths.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WORK="$(mktemp -d)"
WORK="$(cd "$WORK" && pwd -P)"
SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

export LC_ALL=C PYTHONDONTWRITEBYTECODE=1
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME="Lab Author" GIT_AUTHOR_EMAIL="author@example.com"
export GIT_COMMITTER_NAME="Lab Committer" GIT_COMMITTER_EMAIL="committer@example.com"
export GIT_AUTHOR_DATE="2026-11-21T12:00:00Z" GIT_COMMITTER_DATE="2026-11-21T12:00:00Z"

scrub() { sed -e "s|$WORK|<work>|g"; }
yesno() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }

MAIN="$WORK/main"
SLOT1="$WORK/slots/1"
SLOT2="$WORK/slots/2"

# ---------------------------------------------------------------- seed project
git init -q -b main "$MAIN"
mkdir -p "$MAIN/src" "$MAIN/tests" "$MAIN/.gate"
cat > "$MAIN/src/calc.py" <<'EOF'
def add(a, b):
    return a + b
EOF
cat > "$MAIN/tests/test_calc.py" <<'EOF'
import unittest
from src.calc import add


class AddTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_add_negative(self):
        self.assertEqual(add(-1, 1), 0)
EOF
touch "$MAIN/src/__init__.py" "$MAIN/tests/__init__.py"
echo "gate policy: protected paths, secrets, diff budget, tests" > "$MAIN/.gate/policy.txt"
git -C "$MAIN" add -A
git -C "$MAIN" commit -q -m "seed"

echo "== 1. Worktree isolation =="
mkdir -p "$WORK/slots"
git -C "$MAIN" worktree add -q --detach "$SLOT1"
git -C "$MAIN" worktree add -q --detach "$SLOT2"
echo "worktrees: $(git -C "$MAIN" worktree list | wc -l | tr -d ' ') (1 main + 2 linked slots)"
for n in 1 2; do
  gd="$(git -C "$WORK/slots/$n" rev-parse --absolute-git-dir)"
  echo "slot $n git-dir: ${gd#"$MAIN"/}"
done
c1="$(cd "$SLOT1" && cd "$(git rev-parse --git-common-dir)" && pwd -P)"
c2="$(cd "$SLOT2" && cd "$(git rev-parse --git-common-dir)" && pwd -P)"
echo "slots share one common dir: $([ "$c1" = "$c2" ] && echo yes || echo no)"
COMMON="$c1"

echo "edit in slot 1" >> "$SLOT1/src/calc.py"
echo "slot 2 sees slot 1 edit: $([ -n "$(git -C "$SLOT2" status --porcelain)" ] && echo yes || echo no)"
git -C "$SLOT1" checkout -q -- src/calc.py

git -C "$SLOT1" switch -q -c agent/a
if err="$(git -C "$SLOT2" switch agent/a 2>&1)"; then rc=0; else rc=$?; fi
echo "slot 2 switch to slot 1's branch: rc=$rc"
echo "  $(printf '%s' "$err" | scrub)"
git -C "$SLOT1" commit -q --allow-empty -m "agent a work"
echo "slot 2 sees slot 1's branch tip: $(yesno git -C "$SLOT2" rev-parse --verify -q refs/heads/agent/a)"
git -C "$SLOT2" switch -q -c agent/b-work
git -C "$SLOT2" commit -q --allow-empty -m "agent b work"
git -C "$SLOT2" switch -q --detach main

hook_path="$(cd "$SLOT1" && git rev-parse --git-path hooks/pre-commit)"
printf '#!/bin/sh\necho "planted in slot 1, ran in $(basename "$PWD")" > "%s/hook-ran.txt"\n' "$WORK" > "$hook_path"
chmod +x "$hook_path"
git -C "$SLOT2" commit -q --allow-empty -m "innocent commit in slot 2"
echo "hook planted from slot 1 ran on a commit in slot 2: $([ -f "$WORK/hook-ran.txt" ] && echo yes || echo no)"
echo "  $(cat "$WORK/hook-ran.txt")"
rm -f "$hook_path" "$WORK/hook-ran.txt"
git -C "$SLOT2" reset -q --hard main
git -C "$SLOT1" switch -q --detach main

echo
echo "== 2. Seatbelt confinement =="
FAKEHOME="$WORK/fakehome"
GATEDIR="$WORK/gate-private"
SCRATCH="$WORK/scratch"
mkdir -p "$FAKEHOME/.ssh" "$GATEDIR" "$SCRATCH"
echo "EXAMPLE-NOT-A-REAL-KEY" > "$FAKEHOME/.ssh/id_demo"
echo "EXAMPLE-NOT-A-REAL-GATE-KEY" > "$GATEDIR/gate-key"
chmod 600 "$GATEDIR/gate-key"

python3 "$HERE/probe.py" serve "$WORK/port" &
SERVER_PID=$!
for _ in $(seq 1 50); do [ -s "$WORK/port" ] && break; sleep 0.1; done
PORT="$(cat "$WORK/port")"

sed -e "s|@WORKTREE@|$SLOT1|g" -e "s|@COMMON@|$COMMON|g" -e "s|@SCRATCH@|$SCRATCH|g" \
    -e "s|@FAKEHOME@|$FAKEHOME|g" -e "s|@GATEDIR@|$GATEDIR|g" "$HERE/seatbelt.sb.in" > "$WORK/profile.sb"

export AGENT_DEMO_TOKEN="EXAMPLE-NOT-A-REAL-TOKEN"
PROBE_ARGS=("$SLOT1" "$COMMON" "$MAIN" "$FAKEHOME" "$GATEDIR/gate-key" "$PORT")
python3 "$HERE/probe.py" run "${PROBE_ARGS[@]}" > "$WORK/ctrl.txt"
TMPDIR="$SCRATCH" sandbox-exec -f "$WORK/profile.sb" python3 "$HERE/probe.py" run "${PROBE_ARGS[@]}" > "$WORK/sbx.txt"
git -C "$SLOT1" reset -q --hard main
git -C "$SLOT1" switch -q --detach main

printf '%-46s %-12s %s\n' "attempt" "unsandboxed" "sandboxed"
awk -F': ' 'NR==FNR{a[$1]=$2; o[++n]=$1; next}{b[$1]=$2} END{for(i=1;i<=n;i++) printf "%-46s %-12s %s\n", o[i], a[o[i]], b[o[i]]}' "$WORK/ctrl.txt" "$WORK/sbx.txt"

echo "AGENT_DEMO_TOKEN with env -i allowlist: $(env -i PATH="$PATH" HOME="$FAKEHOME" sandbox-exec -f "$WORK/profile.sb" sh -c 'echo ${AGENT_DEMO_TOKEN:-ABSENT}')"

mkdir -p "$WORK/real" && ln -s "$WORK/real" "$WORK/link"
for target in link real; do
  printf '(version 1)\n(allow default)\n(deny file-write* (subpath "%s"))\n' "$WORK/$target" > "$WORK/sym-$target.sb"
  expected=ALLOWED
  if [ "$target" = real ]; then expected='DENIED (EPERM)'; fi
  for route in real link; do
    result="$(sandbox-exec -f "$WORK/sym-$target.sb" python3 "$HERE/probe.py" write-status "$WORK/$route/f")"
    if [ "$result" != "$expected" ]; then
      printf 'deny rule on %s, write via %s: expected %s, got %s\n' "$target" "$route" "$expected" "$result" >&2
      exit 1
    fi
    if [ "$route" = real ]; then
      echo "deny rule on the $target path, write via the real path: ${result%% *}"
    fi
    rm -f "$WORK/real/f"
  done
done

echo
echo "== 3. Ship gate =="
git clone -q --bare "$MAIN" "$WORK/origin.git"
git clone -q --bare "$MAIN" "$WORK/gate.git"
git -C "$WORK/origin.git" config receive.advertisePushOptions true
git -C "$WORK/gate.git" config receive.advertisePushOptions true
git -C "$WORK/gate.git" config gate.origin "$WORK/origin.git"
git -C "$WORK/gate.git" config gate.keyfile "$GATEDIR/gate-key"
printf '#!/bin/sh\nexec python3 "%s/gate.py" origin-hook --key "%s/gate-key"\n' "$HERE" "$GATEDIR" > "$WORK/origin.git/hooks/pre-receive"
printf '#!/bin/sh\nexec python3 "%s/gate.py" hook\n' "$HERE" > "$WORK/gate.git/hooks/pre-receive"
cat > "$WORK/gate.git/hooks/post-receive" <<EOF
#!/bin/sh
origin=\$(git config gate.origin)
key=\$(git config gate.keyfile)
while read old new ref; do
  case "\$new" in 0000000000000000000000000000000000000000) continue;; esac
  att=\$(python3 "$HERE/gate.py" attest --key "\$key" --ref "\$ref" --sha "\$new")
  git push -q -o "attest=\$att" "\$origin" "\$new:\$ref" || exit 1
  echo "forwarded: \$ref"
done
EOF
chmod +x "$WORK/origin.git/hooks/pre-receive" "$WORK/gate.git/hooks/pre-receive" "$WORK/gate.git/hooks/post-receive"
git -C "$SLOT1" remote add gate "$WORK/gate.git"
git -C "$SLOT1" remote add origin "$WORK/origin.git"
printf '#!/bin/sh\nremote="$1"\nif [ "$remote" = origin ]; then echo "local pre-push: push to the gate, not origin" >&2; exit 1; fi\n' > "$COMMON/hooks/pre-push"
chmod +x "$COMMON/hooks/pre-push"

new_branch() { git -C "$SLOT1" switch -q -c "$1" main; }
push() { # push <remote> <branch> [extra flags]; prints the remote's words and the exit code
  local remote="$1" branch="$2"; shift 2
  local out rc
  if out="$(git -C "$SLOT1" push "$@" "$remote" "$branch" 2>&1)"; then rc=0; else rc=$?; fi
  printf '%s\n' "$out" | sed -n 's/^remote: //p;s/^local pre-push: /local pre-push: /p' | scrub | sed -e 's/[[:space:]]*$//' -e 's/^/  | /'
  echo "  push exit code: $rc"
}
has_ref() {
  local rc=0 diagnostic
  diagnostic="$(git -C "$WORK/origin.git" show-ref --exists "refs/heads/$1" 2>&1)" || rc=$?
  case "$rc" in
    0) echo yes ;;
    2) echo no ;;
    *) printf '%s\n' "$diagnostic" >&2; return "$rc" ;;
  esac
}

echo "-- A. good change: new function with a test"
new_branch agent/good
cat >> "$SLOT1/src/calc.py" <<'EOF'


def sub(a, b):
    return a - b
EOF
cat >> "$SLOT1/tests/test_calc.py" <<'EOF'

    def test_sub(self):
        from src.calc import sub
        self.assertEqual(sub(5, 3), 2)
EOF
git -C "$SLOT1" commit -q -am "add sub"
push gate agent/good
echo "  origin has agent/good: $(has_ref agent/good)"

echo "-- B. regression: add() now subtracts"
new_branch agent/regress
sed -i.bak 's/a + b/a - b/' "$SLOT1/src/calc.py" && rm "$SLOT1/src/calc.py.bak"
git -C "$SLOT1" commit -q -am "refactor add"
push gate agent/regress
echo "  origin has agent/regress: $(has_ref agent/regress)"

echo "-- C. agent deletes the failing test instead of fixing it"
new_branch agent/delete-test
git -C "$SLOT1" rm -q tests/test_calc.py
git -C "$SLOT1" commit -q -m "remove flaky test"
push gate agent/delete-test
echo "  origin has agent/delete-test: $(has_ref agent/delete-test)"

echo "-- D. agent commits a credential"
new_branch agent/secret
printf 'aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n' > "$SLOT1/settings.cfg"
git -C "$SLOT1" add settings.cfg
git -C "$SLOT1" commit -q -m "add settings"
push gate agent/secret
echo "  origin has agent/secret: $(has_ref agent/secret)"

echo "-- E. agent edits the gate policy and the CI config"
new_branch agent/edit-gate
echo "gate policy: everything allowed" > "$SLOT1/.gate/policy.txt"
mkdir -p "$SLOT1/.github" && echo "on: push" > "$SLOT1/.github/ci.yml"
git -C "$SLOT1" add -A
git -C "$SLOT1" commit -q -m "tune gate"
push gate agent/edit-gate
edit_gate_ref="$(has_ref agent/edit-gate)"
echo "  origin has agent/edit-gate: $edit_gate_ref" >&2
[ "$edit_gate_ref" = no ]

echo "-- F. skipping the local hook does not skip the gate"
new_branch agent/direct
echo "# note" >> "$SLOT1/src/calc.py"
git -C "$SLOT1" commit -q -am "direct push attempt"
echo "plain push to origin:"
push origin agent/direct
echo "push --no-verify to origin:"
push origin agent/direct --no-verify
echo "  origin has agent/direct: $(has_ref agent/direct)"

echo
echo "== 4. Gate CLI contract =="
git -C "$SLOT1" switch -q --detach main
main_sha="$(git -C "$MAIN" rev-parse HEAD)"
set +e
python3 "$HERE/gate.py" check --repo "$MAIN" --base "$main_sha" --new "$main_sha" --ref refs/heads/agent/noop
echo "exit code: $?"
python3 "$HERE/gate.py" --bogus
echo "exit code: $?"
set -e
