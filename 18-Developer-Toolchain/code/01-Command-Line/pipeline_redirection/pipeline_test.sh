#!/usr/bin/env bash
set -euo pipefail

# The harness has no requirement token for a Bash version, so fail loudly instead of printing a wrong recording.
# Here-documents use pipes from Bash 5.1; the macOS /bin/bash (3.2) has no lastpipe and always uses temporary files.
if [ "${BASH_VERSINFO[0]}" -lt 5 ] || { [ "${BASH_VERSINFO[0]}" -eq 5 ] && [ "${BASH_VERSINFO[1]}" -lt 1 ]; }; then
    echo "pipeline_redirection lab needs Bash 5.1 or later first on PATH, found ${BASH_VERSION}" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="${SCRIPT_DIR}/pipeline_engine"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

# 1. Subshell variable scoping and lastpipe
count=0
printf "line\n" | while read -r _; do
    count=$((count + 1))
done
echo "subshell_isolation: default_count=${count}"

shopt -s lastpipe
set +m
count=0
printf "line\n" | while read -r _; do
    count=$((count + 1))
done
echo "subshell_lastpipe: lastpipe_count=${count}"

# 2. Redirection evaluation order
(echo "STDOUT"; echo "STDERR" >&2) > "${TMPDIR}/out1.log" 2>&1
echo "redir_order_correct: $(tr '\n' ' ' < "${TMPDIR}/out1.log" | sed 's/ *$//')"

captured_err=$((echo "STDOUT"; echo "STDERR" >&2) 2>&1 > "${TMPDIR}/out2.log")
echo "redir_order_inverted_file: $(tr '\n' ' ' < "${TMPDIR}/out2.log" | sed 's/ *$//')"
echo "redir_order_inverted_err: ${captured_err}"

# 3. Pipeline exit status: default vs pipefail vs PIPESTATUS
set +e
set +o pipefail
bash -c "exit 3" | bash -c "exit 0"
echo "status_default: exit=$?"

set -o pipefail
bash -c "exit 3" | bash -c "exit 0"
echo "status_pipefail: exit=$?"

bash -c "exit 11" | bash -c "exit 22" | bash -c "exit 0"
status_all=( "${PIPESTATUS[@]}" )
echo "pipestatus_array: ${status_all[*]}"
set -e

# 4. Here-document tab stripping
tab_line=$(cat <<-EOF
	indented_with_tab
EOF
)
space_line=$(cat <<-EOF
    indented_with_space
EOF
)
echo "heredoc_tab_stripped: \"${tab_line}\""
echo "heredoc_space_preserved: \"${space_line}\""

# 5. Here-document and here-string kernel mechanism (Bash 5.1+): pipe up to 65536 bytes, temporary file above
# Document length counts the trailing newline, so a payload of N bytes is a document of N + 1 bytes.
payload_of() { head -c "$1" /dev/zero | tr '\0' 'A'; }

"${ENGINE}" seek_check heredoc_small <<EOF
small_heredoc_payload
EOF

PAYLOAD=$(payload_of 65535)   # document of 65536 bytes: the largest that still uses a pipe
"${ENGINE}" seek_check heredoc_65536_bytes <<EOF
${PAYLOAD}
EOF

PAYLOAD=$(payload_of 65536)   # document of 65537 bytes: the smallest that falls back to a temporary file
"${ENGINE}" seek_check heredoc_65537_bytes <<EOF
${PAYLOAD}
EOF

PAYLOAD=$(payload_of 65535)
"${ENGINE}" seek_check herestring_65536_bytes <<< "${PAYLOAD}"
PAYLOAD=$(payload_of 65536)
"${ENGINE}" seek_check herestring_65537_bytes <<< "${PAYLOAD}"

# 6. Process substitution seekability
"${ENGINE}" seek_check procsub < <(printf "proc_sub_stream\n")

# 7. Process substitution lifetime. lastpipe is switched off again so every pipeline stage is a subshell.
shopt -u lastpipe

# A consumer that finishes only after the gate file exists, so "not finished yet" is decided by the lab and
# never by a sleep. After the gate opens it pauses 0.2 s before recording "done", so a missing wait is caught.
# Giving up after about 5 s keeps a broken run from leaving a process behind.
# It always runs as a substitution, and `exec >/dev/null` drops its inherited stdout so it cannot hold a pipeline's pipe open.
gated_consumer() {
    local state=$1 gate=$2 i=0
    exec > /dev/null
    cat > /dev/null
    while [ ! -e "${gate}" ] && [ "${i}" -lt 250 ]; do
        sleep 0.02
        i=$((i + 1))
    done
    if [ -e "${gate}" ]; then
        sleep 0.2
        echo done > "${state}"
    else
        echo gave_up > "${state}"
    fi
}

state_of() { if [ -e "$1" ]; then cat "$1"; else echo absent; fi; }

# Poll for a state file so a substitution this shell cannot wait for is still reaped before the lab ends.
await_state() {
    local i=0
    while [ ! -e "$1" ] && [ "${i}" -lt 250 ]; do
        sleep 0.02
        i=$((i + 1))
    done
}

# 7a. One substitution created by a builtin in the parent shell: a bare wait waits for it ($! is its PID).
echo "payload" > >(gated_consumer "${TMPDIR}/s1" "${TMPDIR}/g1")
before_gate=$(state_of "${TMPDIR}/s1")
touch "${TMPDIR}/g1"
wait
echo "procsub_sync_with_wait: before_gate=${before_gate} after_wait=$(state_of "${TMPDIR}/s1")"

# 7b. Two substitutions in one command: a bare wait waits only for the last one.
# The last consumer's gate is already open, so it finishes about 0.2 s after the command returns.
touch "${TMPDIR}/g2b"
tee >(gated_consumer "${TMPDIR}/s2a" "${TMPDIR}/g2a") \
    >(gated_consumer "${TMPDIR}/s2b" "${TMPDIR}/g2b") <<< "payload" > /dev/null
wait
echo "bare_wait_two_substitutions: last=$(state_of "${TMPDIR}/s2b") first=$(state_of "${TMPDIR}/s2a")"
touch "${TMPDIR}/g2a"
await_state "${TMPDIR}/s2a"

# 7c. A substitution expanded inside a pipeline stage belongs to that stage's subshell: a bare wait ignores it.
printf "payload\n" | tee >(gated_consumer "${TMPDIR}/s3" "${TMPDIR}/g3") | cat > /dev/null
wait
echo "bare_wait_pipeline_stage: substitution=$(state_of "${TMPDIR}/s3")"
touch "${TMPDIR}/g3"
await_state "${TMPDIR}/s3"

# 7d. The documented fix: hold the substitution on a descriptor, save $!, close the descriptor, wait for that PID.
exec 3> >(cat > /dev/null; exit 7)
fix_pid=$!
echo "payload" >&3
exec 3>&-
fix_status=0
wait "${fix_pid}" || fix_status=$?
echo "exec_fd_wait_pid_status: ${fix_status}"
