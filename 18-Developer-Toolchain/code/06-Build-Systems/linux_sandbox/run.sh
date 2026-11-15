#!/usr/bin/env bash
# requires: linux
#
# What local build sandboxes isolate, measured without privileges:
#   Parts 1-3 (stage.py): symlink, hardlink, and copy staging of action inputs, and sandbox reuse.
#   Part 4 (netfilter.py): blocking IP sockets with a seccomp-BPF filter.
# The namespace sandbox (ns_sandbox.sh) needs unprivileged user namespaces, which the harness
# container's default seccomp profile refuses, so it is opt-in: LINUX_SANDBOX_LAB_USERNS=1.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

python3 -I "${HERE}/stage.py" "${HERE}/action.py"
python3 -I "${HERE}/netfilter.py"

if [ "${LINUX_SANDBOX_LAB_USERNS:-0}" = "1" ]; then
    bash "${HERE}/ns_sandbox.sh"
fi
