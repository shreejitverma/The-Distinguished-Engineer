#!/usr/bin/env bash
# Needs no harness requirement tag: runs unprivileged on loopback with ssh, sshd, ssh-keygen, ssh-agent, cc, python3.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMPDIR_LAB="$(mktemp -d)"
PIDS=()

cleanup() {
    for pid in "${PIDS[@]}"; do
        kill "${pid}" 2>/dev/null || true
    done
    rm -rf "${TMPDIR_LAB}"
}
trap cleanup EXIT

# 1. Locate sshd binary
SSHD_BIN=""
for candidate in /opt/homebrew/sbin/sshd /usr/local/sbin/sshd /usr/sbin/sshd "$(command -v sshd 2>/dev/null || true)"; do
    if [[ -n "${candidate}" && -x "${candidate}" ]]; then
        SSHD_BIN="${candidate}"
        break
    fi
done
if [[ -z "${SSHD_BIN}" ]]; then
    echo "FATAL: sshd binary not found" >&2
    exit 1
fi

# 2. Pick unprivileged dynamic ports
read -r PORT_TARGET PORT_BASTION < <(python3 -c '
import socket
s1 = socket.socket(); s1.bind(("127.0.0.1", 0)); p1 = s1.getsockname()[1]
s2 = socket.socket(); s2.bind(("127.0.0.1", 0)); p2 = s2.getsockname()[1]
s1.close(); s2.close()
print(p1, p2)
')

CURRENT_USER="$(id -un)"

# ==============================================================================
# Step 1: Establish User CA and Host CA
# ==============================================================================
echo "=== Step 1: Certificate Authority Setup ==="
ssh-keygen -t ed25519 -f "${TMPDIR_LAB}/user_ca" -N "" -C "ca@corp.internal" -q
ssh-keygen -t ed25519 -f "${TMPDIR_LAB}/host_ca" -N "" -C "host-ca@corp.internal" -q
echo "User CA key generated: ED25519"
echo "Host CA key generated: ED25519"

# ==============================================================================
# Step 2: Issue User and Host Certificates
# ==============================================================================
echo ""
echo "=== Step 2: User and Host Certificate Issuance ==="

# Keypairs
ssh-keygen -t ed25519 -f "${TMPDIR_LAB}/alice_key" -N "" -C "alice" -q
ssh-keygen -t ed25519 -f "${TMPDIR_LAB}/bob_key" -N "" -C "bob" -q
ssh-keygen -t ed25519 -f "${TMPDIR_LAB}/charlie_key" -N "" -C "charlie" -q
ssh-keygen -t ed25519 -f "${TMPDIR_LAB}/dave_key" -N "" -C "dave" -q
ssh-keygen -t ed25519 -f "${TMPDIR_LAB}/target_host_key" -N "" -C "target-host" -q
ssh-keygen -t ed25519 -f "${TMPDIR_LAB}/bastion_host_key" -N "" -C "bastion-host" -q

# Alice: admin principal, serial 1001, critical force-command to an existing binary (so only the KRL can deny her), restricted extensions
ssh-keygen -s "${TMPDIR_LAB}/user_ca" \
    -I "alice@corp.internal" \
    -n "${CURRENT_USER},admin" \
    -V "-1h:+1d" \
    -z 1001 \
    -O "force-command=/usr/bin/true" \
    -O clear \
    -O permit-pty \
    -O permit-user-rc \
    "${TMPDIR_LAB}/alice_key.pub" >/dev/null 2>&1

# Bob: developer principal, serial 1002, extensions without agent forwarding
ssh-keygen -s "${TMPDIR_LAB}/user_ca" \
    -I "bob@corp.internal" \
    -n "bob,developer" \
    -V "-1h:+1d" \
    -z 1002 \
    -O no-agent-forwarding \
    -O no-port-forwarding \
    -O permit-pty \
    "${TMPDIR_LAB}/bob_key.pub" >/dev/null 2>&1

# Charlie: expired certificate (-2h to -1h), serial 1003, with the admin principal so only expiry can deny it
ssh-keygen -s "${TMPDIR_LAB}/user_ca" \
    -I "charlie@corp.internal" \
    -n "charlie,admin" \
    -V "-2h:-1h" \
    -z 1003 \
    "${TMPDIR_LAB}/charlie_key.pub" >/dev/null 2>&1

# Dave: active admin certificate, serial 1004 (valid active cert)
ssh-keygen -s "${TMPDIR_LAB}/user_ca" \
    -I "dave@corp.internal" \
    -n "${CURRENT_USER},admin" \
    -V "-1h:+1d" \
    -z 1004 \
    "${TMPDIR_LAB}/dave_key.pub" >/dev/null 2>&1

# Target Host Certificate: signed by host_ca
ssh-keygen -s "${TMPDIR_LAB}/host_ca" \
    -I "host-target.corp.internal" \
    -h \
    -n "target.corp.internal,127.0.0.1" \
    -V "-1h:+52w" \
    -z 2001 \
    "${TMPDIR_LAB}/target_host_key.pub" >/dev/null 2>&1

# Bastion Host Certificate: signed by host_ca
ssh-keygen -s "${TMPDIR_LAB}/host_ca" \
    -I "host-bastion.corp.internal" \
    -h \
    -n "bastion.corp.internal,127.0.0.1" \
    -V "-1h:+52w" \
    -z 2002 \
    "${TMPDIR_LAB}/bastion_host_key.pub" >/dev/null 2>&1

echo "Profile Alice (Admin):"
python3 "${SCRIPT_DIR}/lab_inspect.py" cert "${TMPDIR_LAB}/alice_key-cert.pub" | sed "s/${CURRENT_USER}/alice/g"
echo "Profile Bob (Engineer):"
python3 "${SCRIPT_DIR}/lab_inspect.py" cert "${TMPDIR_LAB}/bob_key-cert.pub"
echo "Profile Charlie (Expired):"
python3 "${SCRIPT_DIR}/lab_inspect.py" cert "${TMPDIR_LAB}/charlie_key-cert.pub"
echo "Target Host Certificate:"
python3 "${SCRIPT_DIR}/lab_inspect.py" cert "${TMPDIR_LAB}/target_host_key-cert.pub"

# ==============================================================================
# Step 3: FIDO2 Hardware Key Simulation
# ==============================================================================
echo ""
echo "=== Step 3: FIDO2 Hardware Key Integration ==="

SHLIB_EXT="dylib"
if [[ "$(uname -s)" != "Darwin" ]]; then
    SHLIB_EXT="so"
fi
"${CC:-cc}" -shared -fPIC -o "${TMPDIR_LAB}/sk_dummy.${SHLIB_EXT}" "${SCRIPT_DIR}/sk_dummy.c"
echo "Compiled FIDO2 Middleware: sk_dummy"

ssh-keygen -t ed25519-sk -w "${TMPDIR_LAB}/sk_dummy.${SHLIB_EXT}" -f "${TMPDIR_LAB}/fido_key" -N "" -q
if [[ "$(awk '{print $1}' "${TMPDIR_LAB}/fido_key.pub")" == "sk-ssh-ed25519@openssh.com" ]] \
    && ssh-keygen -l -f "${TMPDIR_LAB}/fido_key.pub" | grep -qF "(ED25519-SK)"; then
    echo "Enrolled FIDO2 hardware-backed key: ed25519-sk"
else
    echo "Enrolled FIDO2 hardware-backed key: UNEXPECTED_KEY_TYPE"
fi

ssh-keygen -s "${TMPDIR_LAB}/user_ca" \
    -I "fido2-alice@corp.internal" \
    -n "${CURRENT_USER},admin" \
    -V "-1h:+1d" \
    -z 501 \
    "${TMPDIR_LAB}/fido_key.pub" >/dev/null 2>&1

echo "Signed FIDO2 User Certificate:"
python3 "${SCRIPT_DIR}/lab_inspect.py" cert "${TMPDIR_LAB}/fido_key-cert.pub" | sed "s/${CURRENT_USER}/alice/g"

# ==============================================================================
# Step 4: Key Revocation List (KRL) Lifecycle
# ==============================================================================
echo ""
echo "=== Step 4: Key Revocation List (KRL) Lifecycle ==="

# Revoke serial 1001 (Alice)
cat <<EOF > "${TMPDIR_LAB}/krl.spec"
serial: 1001
EOF
ssh-keygen -k -f "${TMPDIR_LAB}/revoked.krl" -s "${TMPDIR_LAB}/user_ca.pub" "${TMPDIR_LAB}/krl.spec" >/dev/null 2>&1

# Update KRL revoking FIDO key by public key fingerprint
FIDO_FP="$(ssh-keygen -l -f "${TMPDIR_LAB}/fido_key.pub" | awk '{print $2}')"
cat <<EOF > "${TMPDIR_LAB}/krl_update.spec"
hash: ${FIDO_FP}
EOF
ssh-keygen -k -u -f "${TMPDIR_LAB}/revoked.krl" -s "${TMPDIR_LAB}/user_ca.pub" "${TMPDIR_LAB}/krl_update.spec" >/dev/null 2>&1

python3 "${SCRIPT_DIR}/lab_inspect.py" krl "${TMPDIR_LAB}/revoked.krl"

# Query checks: ssh-keygen -Q exits 0 for a key not in the KRL and 1 for a revoked key
krl_query() {
    local label="$1" cert="$2" want="$3" rc=0
    ssh-keygen -Q -f "${TMPDIR_LAB}/revoked.krl" "${cert}" >/dev/null 2>&1 || rc=$?
    if [[ "${rc}" -ne "${want}" ]]; then
        echo "${label}: UNEXPECTED (exit ${rc})"
    elif [[ "${rc}" -eq 0 ]]; then
        echo "${label}: VALID (exit 0)"
    else
        echo "${label}: REVOKED (exit ${rc})"
    fi
}
krl_query "Query Cert 1001 (Revoked Serial)" "${TMPDIR_LAB}/alice_key-cert.pub" 1
krl_query "Query Cert 1004 (Active Serial)" "${TMPDIR_LAB}/dave_key-cert.pub" 0
krl_query "Query Cert 501 (Revoked FIDO2 Fingerprint)" "${TMPDIR_LAB}/fido_key-cert.pub" 1

# ==============================================================================
# Step 5: Live OpenSSH Server (sshd) Access Enforcement
# ==============================================================================
echo ""
echo "=== Step 5: Live OpenSSH Server (sshd) Access Enforcement ==="

# Authorized principals for target host: only 'admin'
echo "admin" > "${TMPDIR_LAB}/principals_${CURRENT_USER}"
echo "admin" > "${TMPDIR_LAB}/principals_bastion_${CURRENT_USER}"

TARGET_LOG="${TMPDIR_LAB}/sshd_target.log"
BASTION_LOG="${TMPDIR_LAB}/sshd_bastion.log"
: > "${TARGET_LOG}"
: > "${BASTION_LOG}"

# The target allows agent forwarding (the control); the bastion does not.
# Forwarded agent sockets go under the lab directory when this sshd supports AgentSocketPath (added in OpenSSH 10.6);
# otherwise sshd uses its default location (~/.ssh/agent since 10.1, /tmp/ssh-* before).
mkdir "${TMPDIR_LAB}/af"
cat <<EOF > "${TMPDIR_LAB}/sshd_target_config"
Port ${PORT_TARGET}
HostKey ${TMPDIR_LAB}/target_host_key
HostCertificate ${TMPDIR_LAB}/target_host_key-cert.pub
PidFile ${TMPDIR_LAB}/sshd_target.pid
TrustedUserCAKeys ${TMPDIR_LAB}/user_ca.pub
AuthorizedPrincipalsFile ${TMPDIR_LAB}/principals_%u
RevokedKeys ${TMPDIR_LAB}/revoked.krl
UsePAM no
StrictModes no
AllowTcpForwarding yes
AllowAgentForwarding yes
LogLevel VERBOSE
EOF

# Append an option to a config only when this sshd accepts it (older releases reject unknown keywords).
add_if_supported() {
    local cfg="$1" line="$2"
    cp "${cfg}" "${cfg}.probe"
    echo "${line}" >> "${cfg}.probe"
    if "${SSHD_BIN}" -t -f "${cfg}.probe" >/dev/null 2>&1; then
        echo "${line}" >> "${cfg}"
    fi
}
# OpenSSH 9.8+ penalizes a source address after failed logins and then drops its connections (PerSourcePenalties);
# every probe here comes from 127.0.0.1, so the penalty would turn later probes into false denials.
add_if_supported "${TMPDIR_LAB}/sshd_target_config" "PerSourcePenalties no"
add_if_supported "${TMPDIR_LAB}/sshd_target_config" "AgentSocketPath shared:${TMPDIR_LAB}/af"

"${SSHD_BIN}" -f "${TMPDIR_LAB}/sshd_target_config" -D -E "${TARGET_LOG}" &
PIDS+=($!)

HOST_CA_PUB="$(cat "${TMPDIR_LAB}/host_ca.pub")"
cat <<EOF > "${TMPDIR_LAB}/known_hosts"
@cert-authority [127.0.0.1]:${PORT_TARGET},target.corp.internal ${HOST_CA_PUB}
@cert-authority [127.0.0.1]:${PORT_BASTION},bastion.corp.internal ${HOST_CA_PUB}
EOF

# A known_hosts that trusts a different CA: host certificate verification must reject the target
ssh-keygen -t ed25519 -f "${TMPDIR_LAB}/rogue_ca" -N "" -C "rogue-ca" -q
cat <<EOF > "${TMPDIR_LAB}/known_hosts_rogue"
@cert-authority [127.0.0.1]:${PORT_TARGET},target.corp.internal $(cat "${TMPDIR_LAB}/rogue_ca.pub")
EOF

cat <<EOF > "${TMPDIR_LAB}/ssh_client_config"
Host *
    StrictHostKeyChecking yes
    UserKnownHostsFile ${TMPDIR_LAB}/known_hosts
    GlobalKnownHostsFile /dev/null
    IdentitiesOnly yes
    IdentityAgent none
    LogLevel ERROR
    BatchMode yes
EOF

wait_for_port() {
    for _ in $(seq 1 50); do
        if python3 -c "import socket,sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), 1).close()" "$1" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done
    echo "FATAL: nothing listening on port $1" >&2
    return 1
}
wait_for_port "${PORT_TARGET}"

# Probe the target with one identity and compare the client exit code, the command output, and the server log reason.
# usage: target_probe <label> <key> <token> <want-rc> <log-pattern> <word-on-exit-0> <word-on-exit-255>
target_probe() {
    local label="$1" key="$2" token="$3" want="$4" pattern="$5" word_ok="$6" word_denied="$7"
    local mark rc=0 out
    mark=$(( $(wc -l < "${TARGET_LOG}") ))
    out="$(ssh -F "${TMPDIR_LAB}/ssh_client_config" -p "${PORT_TARGET}" -i "${key}" \
        "${CURRENT_USER}@127.0.0.1" "echo ${token}" 2>/dev/null)" || rc=$?
    local expected_out=""
    [[ "${want}" -eq 0 ]] && expected_out="${token}"
    if [[ "${rc}" -eq "${want}" && "${out}" == "${expected_out}" ]] \
        && tail -n +$((mark + 1)) "${TARGET_LOG}" | grep -qF -- "${pattern}"; then
        if [[ "${want}" -eq 0 ]]; then
            echo "${label}: ${word_ok} (exit ${rc})"
        else
            echo "${label}: ${word_denied} (exit ${rc})"
        fi
    else
        echo "${label}: UNEXPECTED (exit ${rc})"
    fi
}

target_probe "Connect with Valid Active Cert (Dave, Admin Principal)" "${TMPDIR_LAB}/dave_key" DAVE_AUTH_OK 0 \
    'Accepted certificate ID "dave@corp.internal" (serial 1004)' SUCCESS DENIED
target_probe "Connect with Unauthorized Principal (Bob, Developer)" "${TMPDIR_LAB}/bob_key" BOB_AUTH_OK 255 \
    "Certificate does not contain an authorized principal" SUCCESS DENIED
target_probe "Connect with Expired Cert (Charlie, Admin Principal)" "${TMPDIR_LAB}/charlie_key" CHARLIE_AUTH_OK 255 \
    "Certificate invalid: expired" SUCCESS DENIED
target_probe "Connect with Revoked Cert (Alice, Serial 1001)" "${TMPDIR_LAB}/alice_key" ALICE_AUTH_OK 255 \
    "revoked by file ${TMPDIR_LAB}/revoked.krl" SUCCESS DENIED

# Host certificate verification: a client that trusts a different host CA must refuse the target
rc=0
ssh -F "${TMPDIR_LAB}/ssh_client_config" -o "UserKnownHostsFile=${TMPDIR_LAB}/known_hosts_rogue" \
    -p "${PORT_TARGET}" -i "${TMPDIR_LAB}/dave_key" "${CURRENT_USER}@127.0.0.1" "echo HOSTCA_OK" >/dev/null 2>&1 || rc=$?
if [[ "${rc}" -eq 255 ]]; then
    echo "Connect to Target Trusting a Rogue Host CA (Dave): REFUSED (exit ${rc})"
else
    echo "Connect to Target Trusting a Rogue Host CA (Dave): UNEXPECTED (exit ${rc})"
fi

# ==============================================================================
# Step 6: Bastion Architecture via ProxyJump (Zero Agent Forwarding)
# ==============================================================================
echo ""
echo "=== Step 6: Bastion Architecture via ProxyJump (Zero Agent Forwarding) ==="

cat <<EOF > "${TMPDIR_LAB}/sshd_bastion_config"
Port ${PORT_BASTION}
HostKey ${TMPDIR_LAB}/bastion_host_key
HostCertificate ${TMPDIR_LAB}/bastion_host_key-cert.pub
PidFile ${TMPDIR_LAB}/sshd_bastion.pid
TrustedUserCAKeys ${TMPDIR_LAB}/user_ca.pub
AuthorizedPrincipalsFile ${TMPDIR_LAB}/principals_bastion_%u
UsePAM no
StrictModes no
AllowTcpForwarding yes
AllowAgentForwarding no
LogLevel VERBOSE
EOF
add_if_supported "${TMPDIR_LAB}/sshd_bastion_config" "PerSourcePenalties no"

"${SSHD_BIN}" -f "${TMPDIR_LAB}/sshd_bastion_config" -D -E "${BASTION_LOG}" &
PIDS+=($!)
wait_for_port "${PORT_BASTION}"

cat <<EOF >> "${TMPDIR_LAB}/ssh_client_config"

Host bastion
    HostName 127.0.0.1
    Port ${PORT_BASTION}
    User ${CURRENT_USER}
    IdentityFile ${TMPDIR_LAB}/dave_key

Host target
    HostName 127.0.0.1
    Port ${PORT_TARGET}
    User ${CURRENT_USER}
    IdentityFile ${TMPDIR_LAB}/dave_key
    ProxyJump bastion

Host target-direct
    HostName 127.0.0.1
    Port ${PORT_TARGET}
    User ${CURRENT_USER}
    IdentityFile ${TMPDIR_LAB}/dave_key
EOF

# The tunnel must run end to end, the client must have used ssh -W, and the bastion must have authenticated Dave
rc=0
out="$(ssh -v -F "${TMPDIR_LAB}/ssh_client_config" target "echo PROXYJUMP_TUNNEL_SUCCESS" 2>"${TMPDIR_LAB}/proxy_client.log")" || rc=$?
if [[ "${rc}" -eq 0 && "${out}" == "PROXYJUMP_TUNNEL_SUCCESS" ]]; then
    echo "ProxyJump End-to-End Tunnel: SUCCESS (exit ${rc})"
else
    echo "ProxyJump End-to-End Tunnel: FAILED (exit ${rc})"
fi
if grep -F "Executing proxy command" "${TMPDIR_LAB}/proxy_client.log" | grep -qE "^debug1: Executing proxy command: exec ssh .* -W '\[127\.0\.0\.1\]:${PORT_TARGET}' bastion"; then
    echo "Client Encapsulation: ssh -W"
else
    echo "Client Encapsulation: UNEXPECTED_NOT_SSH_W"
fi
if grep -qF 'Accepted certificate ID "dave@corp.internal" (serial 1004)' "${BASTION_LOG}"; then
    echo "Bastion Authenticated the Certificate: yes"
else
    echo "Bastion Authenticated the Certificate: UNEXPECTED_NO"
fi

# Agent forwarding probe: a real agent holds Dave's key and the client asks to forward it (ForwardAgent yes).
# The target allows forwarding (control: the socket must appear), the bastion forbids it (the socket must not).
AGENT_SOCK="${TMPDIR_LAB}/agent.sock"
ssh-agent -a "${AGENT_SOCK}" -D >/dev/null 2>&1 &
PIDS+=($!)
for _ in $(seq 1 50); do
    [[ -S "${AGENT_SOCK}" ]] && break
    sleep 0.1
done
SSH_AUTH_SOCK="${AGENT_SOCK}" ssh-add "${TMPDIR_LAB}/dave_key" >/dev/null 2>&1

agent_state() {
    local host="$1" out rc=0
    out="$(ssh -F "${TMPDIR_LAB}/ssh_client_config" -o "IdentityAgent=${AGENT_SOCK}" -o ForwardAgent=yes "${host}" \
        'echo "AGENT_SOCK=${SSH_AUTH_SOCK:-NONE}"' 2>/dev/null)" || rc=$?
    if [[ "${rc}" -ne 0 ]]; then
        echo "CONNECT_FAILED"
    elif [[ "${out}" == "AGENT_SOCK=NONE" ]]; then
        echo "AGENT_SOCKET_ABSENT"
    else
        echo "AGENT_SOCKET_PRESENT"
    fi
}
echo "Target Agent Forwarding State (AllowAgentForwarding yes, control): $(agent_state target-direct)"
echo "Bastion Agent Forwarding State (AllowAgentForwarding no): $(agent_state bastion)"
