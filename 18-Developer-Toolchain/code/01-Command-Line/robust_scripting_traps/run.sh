#!/usr/bin/env bash
# Needs (checked below, since the harness `requires:` vocabulary has no token for them):
# Bash 4.4+ first on PATH, python3, and ShellCheck 0.11.0 (the golden file pins its message text).
set -euo pipefail

export LC_ALL=C

die() {
    echo "lab precondition failed: $*" >&2
    exit 2
}

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
    die "needs Bash 4.4+ for inherit_errexit, running ${BASH_VERSION}"
fi
command -v python3 >/dev/null 2>&1 || die "python3 not found"
command -v shellcheck >/dev/null 2>&1 || die "shellcheck not found"
sc_version=$(shellcheck --version | sed -n 's/^version: //p')
[ "${sc_version}" = "0.11.0" ] || die "needs ShellCheck 0.11.0, found '${sc_version}'"

echo "=== Case 1: local var=\$(cmd) Return Code Masking (SC2155) ==="
# Anti-pattern: local var=$(cmd) returns 0 from the local builtin, masking $(false)
test_masking() {
    local masked_var=$(false)
    echo "Masked assignment: exit_code=$?, val='${masked_var}'"
}
test_masking

# Robust pattern: separate declaration from assignment
test_unmasked() {
    local unmasked_var
    if ! unmasked_var=$(false); then
        echo "Unmasked assignment: caught command failure explicitly"
    fi
}
test_unmasked

echo "=== Case 2: errexit Suppression in Conditionals ==="
conditional_callee() {
    echo "callee: step 1 (before failure)"
    false
    echo "callee: step 2 (errexit was suppressed by caller condition)"
    return 0
}

# In POSIX and Bash, errexit is suppressed in the condition of if/while/until
if conditional_callee; then
    echo "caller: condition branch taken despite callee failure"
fi

# Contrast: the same function called as a plain command under errexit stops at the failure
echo "Same callee called directly under set -e:"
set +e
"${BASH}" -c "set -e; $(declare -f conditional_callee); conditional_callee; echo 'callee: returned normally'"
direct_rc=$?
set -e
echo "Direct call aborted with exit code ${direct_rc}"

echo "=== Case 3: inherit_errexit in Command Substitutions ==="
echo "Default subshell command substitution:"
"${BASH}" -c 'set -e; out=$(false; echo "survived_cmdsub"); echo "out=${out}"'

echo "With inherit_errexit enabled:"
set +e
# Exit 99 means shopt rejected the option, so a failure here cannot pass as the abort under test.
"${BASH}" -c 'set -e; shopt -s inherit_errexit || exit 99; out=$(false; echo "survived_cmdsub"); echo "out=${out}"'
inherit_rc=$?
set -e
[ "${inherit_rc}" -ne 99 ] || die "shopt -s inherit_errexit was rejected"
echo "Command substitution aborted with exit code ${inherit_rc}"

echo "=== Case 4: Signal Traps and Cooperative Re-raising ==="
# Each variant raises SIGINT against its own PID. The Python parent reports how the child
# ended: killed by the signal (WIFSIGNALED) or a normal exit (WIFEXITED). In non-interactive
# environments (background jobs, CI runners), POSIX XCU 2.12 sets SIGINT to SIG_IGN and XCU 2.15
# forbids trapping it, so the parent first restores SIG_DFL, which the child inherits.
signal_probe=$(
    cat <<'PY_EOF'
import signal, subprocess, sys
signal.signal(signal.SIGINT, signal.SIG_DFL)
rc = subprocess.run([sys.argv[1], "-c", sys.argv[2]]).returncode
if rc < 0:
    print("child terminated by " + signal.Signals(-rc).name + " (WIFSIGNALED)")
else:
    print("child exited with status " + str(rc) + " (WIFEXITED)")
PY_EOF
)
signal_body=$(
    cat <<'SH_EOF'
tmp="allocated_resource"
cleanup() {
    echo "trap EXIT: cleaning up ${tmp}"
}
trap cleanup EXIT
__INT_TRAP__
kill -s INT $$
echo "unreachable"
SH_EOF
)
run_signal_variant() {
    local label="$1" int_trap="$2"
    echo "${label}"
    python3 -I -c "${signal_probe}" "${BASH}" "${signal_body/__INT_TRAP__/${int_trap}}"
}
run_signal_variant "Variant A, re-raise handler:" "trap 'trap - INT; kill -s INT \$\$' INT"
run_signal_variant "Variant B, anti-pattern exit 1 handler:" "trap 'exit 1' INT"
run_signal_variant "Variant C, no INT trap:" ":"

echo "=== Case 5: Atomic File Replacement and Symlink Pitfall ==="
SCRATCH_DIR=$(mktemp -d /tmp/robust_lab.XXXXXX)
trap 'rm -rf "${SCRATCH_DIR}"' EXIT

# Safe atomic file update via mktemp in target directory and rename(2)
echo "initial_payload" > "${SCRATCH_DIR}/data.txt"
tmp_data=$(mktemp "${SCRATCH_DIR}/data.tmp.XXXXXX")
echo "updated_payload" > "${tmp_data}"
mv -f "${tmp_data}" "${SCRATCH_DIR}/data.txt"
echo "Atomic file content: $(cat "${SCRATCH_DIR}/data.txt")"

# Symlink directory update pitfall: plain mv follows the existing link into the directory
mkdir -p "${SCRATCH_DIR}/naive/release_a" "${SCRATCH_DIR}/naive/release_b"
ln -s "release_a" "${SCRATCH_DIR}/naive/current"
ln -s "release_b" "${SCRATCH_DIR}/naive/current.tmp"
mv -f "${SCRATCH_DIR}/naive/current.tmp" "${SCRATCH_DIR}/naive/current"
if [ -L "${SCRATCH_DIR}/naive/release_a/current.tmp" ]; then
    deposited=yes
else
    deposited=no
fi
echo "Naive mv: current -> $(readlink "${SCRATCH_DIR}/naive/current"), link deposited inside release_a: ${deposited}"

mkdir -p "${SCRATCH_DIR}/release_a" "${SCRATCH_DIR}/release_b"
ln -s "release_a" "${SCRATCH_DIR}/current"
ln -s "release_b" "${SCRATCH_DIR}/current.tmp"

# On macOS/BSD mv -h prevents following symlink to directory; on GNU mv -T does the same
if mv -h -f "${SCRATCH_DIR}/current.tmp" "${SCRATCH_DIR}/current" 2>/dev/null; then
    :
elif mv -T -f "${SCRATCH_DIR}/current.tmp" "${SCRATCH_DIR}/current" 2>/dev/null; then
    :
else
    die "neither mv -h nor mv -T replaced the symlink"
fi
echo "Symlink atomically updated to: $(readlink "${SCRATCH_DIR}/current")"
if [ -e "${SCRATCH_DIR}/release_a/current.tmp" ]; then
    deposited=yes
else
    deposited=no
fi
echo "Link deposited inside release_a: ${deposited}"

echo "=== Case 6: ShellCheck Static Analysis ==="
cat << 'BAD_EOF' > "${SCRATCH_DIR}/anti_pattern.sh"
#!/usr/bin/env bash
tmp=$(mktemp)
trap "rm -f $tmp" EXIT
get_data() {
    local payload=$(false)
    echo $payload
}
get_data
BAD_EOF

echo "Running ShellCheck on anti-pattern script:"
set +e
shellcheck -f gcc "${SCRATCH_DIR}/anti_pattern.sh" | sed "s|${SCRATCH_DIR}/||" | cut -d: -f2-
bad_sc_rc=$?
set -e
echo "Anti-pattern exit code: ${bad_sc_rc}"

cat << 'GOOD_EOF' > "${SCRATCH_DIR}/remediated.sh"
#!/usr/bin/env bash
set -euo pipefail
tmp=""
cleanup() {
    if [[ -n "${tmp:-}" && -f "${tmp}" ]]; then
        rm -f "${tmp}"
    fi
}
trap cleanup EXIT
tmp=$(mktemp)

get_data() {
    local payload
    payload="valid_payload"
    printf '%s\n' "${payload}"
}
get_data
GOOD_EOF

echo "Running ShellCheck on remediated script:"
set +e
shellcheck "${SCRATCH_DIR}/remediated.sh"
good_sc_rc=$?
set -e
echo "Remediated exit code: ${good_sc_rc}"
