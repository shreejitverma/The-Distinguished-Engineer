#!/usr/bin/env bash
# requires: macos
# Runs Steps 1 to 5 of Command-Line-Decision-Records.md in a scratch git repository.
# Every gate file is extracted verbatim from the note's heredocs, so the lab runs the code the note prints.
# macos because the pipeline section compares the host bash with macOS /bin/bash 3.2.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTE="${SCRIPT_DIR}/../../../01-Command-Line/Command-Line-Decision-Records.md"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT
REPO="${WORKDIR}/repo"

export LC_ALL=C GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
GIT_ID=(-c user.name=lab -c user.email=lab@example.invalid -c commit.gpgsign=false)

extract() { python3 "${SCRIPT_DIR}/extract_heredocs.py" "${NOTE}" "${REPO}" "$@"; }
# Run a gate with both streams on stdout, then print its exit status.
gate() {
    local rc=0
    "$@" 2>&1 || rc=$?
    echo "exit status: ${rc}"
}

mkdir -p "${REPO}"
cd "${REPO}"
git init -q

echo "=== Step 1: linter on an empty registry ==="
extract docs/decisions/adr-template.md docs/decisions/index.md docs/decisions/lint-adrs.py
gate python3 docs/decisions/lint-adrs.py

echo ""
echo "=== Step 2: audit on the scaffolded repository ==="
extract docs/decisions/0001-tooling-language-boundary.md scripts/audit-tooling-language.sh
gate bash scripts/audit-tooling-language.sh

echo ""
echo "=== Step 5: pre-commit hook on the clean repository ==="
extract docs/decisions/0002-workstation-environment-standardization.md flake.nix .envrc mise.toml \
    docs/decisions/0003-zero-trust-remote-access.md .githooks/pre-commit
chmod +x .githooks/pre-commit
git config core.hooksPath .githooks
gate bash .githooks/pre-commit
git add -A
if git "${GIT_ID[@]}" commit -q -m "Adopt ADR-001 to ADR-003" >/dev/null 2>&1; then
    echo "git commit through the hook accepted: True"
else
    echo "git commit through the hook accepted: False"
fi

echo ""
echo "=== Step 5: pre-commit hook with ForwardAgent yes ==="
mkdir -p ssh
printf 'Host legacy-bastion\n    ForwardAgent yes\n' > ssh/config.bad
gate bash .githooks/pre-commit
git add ssh/config.bad
if git "${GIT_ID[@]}" commit -q -m "Add legacy bastion" >/dev/null 2>&1; then
    echo "git commit through the hook rejected: False"
else
    echo "git commit through the hook rejected: True"
fi
echo "commits on HEAD: $(git rev-list --count HEAD)"
git rm -q --cached ssh/config.bad
rm -r ssh

echo ""
echo "=== Step 1 linter: invalid status and missing Confirmation ==="
ADR1=docs/decisions/0001-tooling-language-boundary.md
sed 's/^accepted$/approved/' "${ADR1}" > docs/decisions/0004-bad-status.md
sed '/^## Confirmation$/d' "${ADR1}" > docs/decisions/0005-no-confirmation.md
gate python3 docs/decisions/lint-adrs.py
rm docs/decisions/0004-bad-status.md docs/decisions/0005-no-confirmation.md

echo ""
echo "=== Step 2 audit: skipped vendor directory, then a 120-line script with declare -A ==="
mkdir -p node_modules/vendored tools
for i in $(seq 1 150); do echo "echo ${i}"; done > node_modules/vendored/big.sh
gate bash scripts/audit-tooling-language.sh
{
    echo '#!/usr/bin/env bash'
    echo 'declare -A counts'
    for i in $(seq 1 119); do echo "counts[k${i}]=${i}"; done
} > tools/legacy-report.sh
gate bash scripts/audit-tooling-language.sh

echo ""
echo "=== Step 4: certificate from the note's recipe ==="
mkdir -p "${WORKDIR}/ssh-ca-test"
cd "${WORKDIR}/ssh-ca-test"
ssh-keygen -q -N "" -t ed25519 -f ca_user_key -C "Corporate User CA"
ssh-keygen -q -N "" -t ed25519 -f dev_user_key -C "alice@corp.internal"
ssh-keygen -s ca_user_key \
           -I "alice@corp.internal" \
           -n "dev-user,alice" \
           -V "+8h" \
           -z "2026110701" \
           -O clear \
           -O permit-pty \
           -O permit-port-forwarding \
           dev_user_key.pub 2>/dev/null
ssh-keygen -L -f dev_user_key-cert.pub > listing.txt
sed -E -e 's/SHA256:[A-Za-z0-9+\/]+/SHA256:.../' \
       -e 's/Valid: from [^ ]+ to [^ ]+$/Valid: from START to END/' listing.txt
python3 - listing.txt <<'PY'
import re, sys
from datetime import datetime, timedelta

start, end = re.search(r"Valid: from (\S+) to (\S+)$", open(sys.argv[1]).read(), re.MULTILINE).groups()
span = datetime.fromisoformat(end) - datetime.fromisoformat(start)
print(f"validity span within 8h to 8h05m: {timedelta(hours=8) <= span <= timedelta(hours=8, minutes=5)}")
PY

echo ""
echo "=== Step 4: the same key signed without -O clear and without -V ==="
cp dev_user_key.pub default_key.pub
ssh-keygen -s ca_user_key -I "alice@corp.internal" -n "dev-user" default_key.pub 2>/dev/null
ssh-keygen -L -f default_key-cert.pub | sed -n -e '/Valid:/p' -e '/Extensions:/,$p'

echo ""
echo "=== Interview: while-read pipeline under set -euo pipefail ==="
cat > "${WORKDIR}/count.sh" <<'SNIPPET'
#!/usr/bin/env bash
set -euo pipefail

count=0
echo "apple banana cherry" | tr ' ' '\n' | while read -r item; do
    ((count++))
done
echo "Count: $count"
SNIPPET
# shellcheck disable=SC2016 # the replacement is shell text for the snippet, not for this script
sed 's/((count++))/count=$((count + 1))/' "${WORKDIR}/count.sh" > "${WORKDIR}/count-fixed.sh"
for shell in bash /bin/bash /bin/zsh; do
    for script in count.sh count-fixed.sh; do
        rc=0
        out="$("${shell}" "${WORKDIR}/${script}" 2>/dev/null)" || rc=$?
        echo "${shell} ${script}: stdout [${out}], exit status ${rc}"
    done
done
