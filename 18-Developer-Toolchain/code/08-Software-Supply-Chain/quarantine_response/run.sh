#!/usr/bin/env bash
# Needs only python3 and runs offline, so it declares no requirements.
# Offline incident-response lab: blast radius, exposure window, secret triage,
# egress diff, provenance regression, override plus rollback verification, OpenVEX.
# Every input is a synthetic fixture built inside quarantine_response.py; no network.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
exec python3 "${SCRIPT_DIR}/quarantine_response.py"
