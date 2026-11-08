#!/usr/bin/env bash
set -euo pipefail

# Force deterministic locale and timezone
export LC_ALL=C
export TZ=UTC

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

# Generate throwaway CA, host, and user keys for this run only
for name in ca_key host_key user_key; do
    ssh-keygen -q -t ed25519 -N "" -C "lab-$name" -f "$WORK_DIR/$name"
done
CA_FP=$(ssh-keygen -l -E sha256 -f "$WORK_DIR/ca_key.pub" | cut -d' ' -f2)

# Fresh keys change every fingerprint per run: mask the CA's as one token and every other key's as another
mask_fingerprints() {
    sed -e "s|$CA_FP|SHA256:<ca-fingerprint>|g" -e 's|SHA256:[A-Za-z0-9+/]\{43\}|SHA256:<key-fingerprint>|g'
}

# Step 1: Mint user certificate (pinned UTC validity: 2026-11-01 to 2026-11-02, serial 42)
ssh-keygen -s "$WORK_DIR/ca_key" \
    -I "alice-corp-session-42" \
    -n "alice,dev-operator" \
    -V "20261101000000Z:20261102000000Z" \
    -z 42 \
    "$WORK_DIR/user_key.pub" >/dev/null 2>&1

# Step 2: Mint host certificate (pinned UTC validity: 1 year, serial 101; host certs carry no critical options)
ssh-keygen -s "$WORK_DIR/ca_key" \
    -h \
    -I "bastion-corp-host-101" \
    -n "bastion.corp.internal,10.0.0.1" \
    -V "20261101000000Z:20271101000000Z" \
    -z 101 \
    "$WORK_DIR/host_key.pub" >/dev/null 2>&1

echo "=== 1. Minted User Certificate Metadata ==="
ssh-keygen -L -f "$WORK_DIR/user_key-cert.pub" | tail -n +2 | mask_fingerprints

echo ""
echo "=== 2. Minted Host Certificate Metadata ==="
ssh-keygen -L -f "$WORK_DIR/host_key-cert.pub" | tail -n +2 | mask_fingerprints

echo ""
python3 cert_validator.py wire "$WORK_DIR/user_key-cert.pub"

# Section 4: enforcement by OpenSSH itself. `ssh-keygen -Y verify` runs the same
# sshkey_cert_check_authority() path (CA match, principal, validity window, cert type)
# that sshd uses, with the clock pinned by -Overify-time.
echo ""
echo "=== 4. OpenSSH Certificate Enforcement (ssh-keygen -Y verify, pinned clock) ==="
CA_PUB=$(cut -d' ' -f1,2 "$WORK_DIR/ca_key.pub")
echo "alice,dev-operator,root cert-authority $CA_PUB" > "$WORK_DIR/allowed_signers"
echo "bastion.corp.internal cert-authority $CA_PUB" > "$WORK_DIR/allowed_hosts"
echo "payload" > "$WORK_DIR/msg.txt"
ssh-keygen -Y sign -f "$WORK_DIR/user_key-cert.pub" -n lab "$WORK_DIR/msg.txt" </dev/null >/dev/null 2>&1
cp "$WORK_DIR/msg.txt.sig" "$WORK_DIR/user.sig"
rm -f "$WORK_DIR/msg.txt.sig"
ssh-keygen -Y sign -f "$WORK_DIR/host_key-cert.pub" -n lab "$WORK_DIR/msg.txt" </dev/null >/dev/null 2>&1
mv "$WORK_DIR/msg.txt.sig" "$WORK_DIR/host.sig"

# enforce <label> <signers file> <principal> <signature> <verify-time>
enforce() {
    local label=$1 signers=$2 principal=$3 sig=$4 when=$5 rc=0 err
    err=$(ssh-keygen -Y verify -f "$signers" -I "$principal" -n lab -s "$sig" \
        -Overify-time="$when" <"$WORK_DIR/msg.txt" 2>&1 >/dev/null) || rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "$label: ACCEPT"
    else
        echo "$label: REJECT - $(printf '%s\n' "$err" | sed -n 's/.*certificate not authorized: //p' | head -n 1)"
    fi
}
enforce "Check 1 (user cert, principal 'alice', inside window)" "$WORK_DIR/allowed_signers" alice "$WORK_DIR/user.sig" 20261101120000
enforce "Check 2 (user cert, principal 'root' not on the cert)" "$WORK_DIR/allowed_signers" root "$WORK_DIR/user.sig" 20261101120000
enforce "Check 3 (user cert, after valid_before)" "$WORK_DIR/allowed_signers" alice "$WORK_DIR/user.sig" 20261103000000
enforce "Check 4 (user cert, before valid_after)" "$WORK_DIR/allowed_signers" alice "$WORK_DIR/user.sig" 20261031000000
enforce "Check 5 (host cert presented as a user cert)" "$WORK_DIR/allowed_hosts" bastion.corp.internal "$WORK_DIR/host.sig" 20261101120000

echo ""
echo "=== 5. Tampered Key ID Byte ==="
python3 cert_validator.py tamper "$WORK_DIR/user_key-cert.pub" "$WORK_DIR/tampered-cert.pub"
TAMPER_ERR=$(ssh-keygen -L -f "$WORK_DIR/tampered-cert.pub" 2>&1 >/dev/null) || true
echo "ssh-keygen -L on the tampered certificate: ${TAMPER_ERR#*: }"

echo ""
echo "=== 6. OpenSSH Key Revocation List (KRL) Enforcement ==="
echo "serial: 42" > "$WORK_DIR/krl_spec.txt"
ssh-keygen -k -f "$WORK_DIR/revoked.krl" -s "$WORK_DIR/ca_key.pub" "$WORK_DIR/krl_spec.txt" >/dev/null 2>&1

# Test user certificate (serial 42) against KRL
STATUS_USER=0
ssh-keygen -Q -f "$WORK_DIR/revoked.krl" "$WORK_DIR/user_key-cert.pub" >/dev/null 2>&1 || STATUS_USER=$?
if [ "$STATUS_USER" -ne 0 ]; then
    echo "Query user certificate (serial 42): REVOKED (exit $STATUS_USER)"
else
    echo "Query user certificate (serial 42): OK (exit $STATUS_USER)"
fi

# Test host certificate (serial 101) against KRL
STATUS_HOST=0
ssh-keygen -Q -f "$WORK_DIR/revoked.krl" "$WORK_DIR/host_key-cert.pub" >/dev/null 2>&1 || STATUS_HOST=$?
if [ "$STATUS_HOST" -eq 0 ]; then
    echo "Query host certificate (serial 101): OK (exit $STATUS_HOST)"
else
    echo "Query host certificate (serial 101): REVOKED (exit $STATUS_HOST)"
fi
