#!/usr/bin/env bash
# requires: network
#
# Dependency confusion against a multi-index Python resolver: exploit it
# with pip's default multi-index merge, then show two remediations and one
# remediation that looks safe but is not (a naive pip-to-uv flag migration).
# Everything below talks only to 127.0.0.1; TOOLCHAIN_LABS_NETWORK gates it
# because it opens listening sockets and drives real pip/uv resolution,
# matching the harness's network-requirement contract even though no byte
# leaves the loopback interface.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(mktemp -d)"
INTERNAL_PORT=51730
PUBLIC_PORT=51731
INTERNAL_PID=""
PUBLIC_PID=""

cleanup() {
  [ -n "${INTERNAL_PID}" ] && kill "${INTERNAL_PID}" >/dev/null 2>&1 || true
  [ -n "${PUBLIC_PID}" ] && kill "${PUBLIC_PID}" >/dev/null 2>&1 || true
  wait >/dev/null 2>&1 || true
  rm -rf "${ROOT}"
}
trap cleanup EXIT

command -v uv >/dev/null 2>&1 || {
  echo "ERROR: this lab also exercises uv; install it (https://docs.astral.sh/uv/getting-started/installation/)" >&2
  exit 1
}

# Refuse to run if something else already listens on a fixed port: wait_for
# would otherwise accept that foreign listener as one of our registries.
for port in "${INTERNAL_PORT}" "${PUBLIC_PORT}"; do
  if python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1]))) == 0 else 1)" "${port}"; then
    echo "ERROR: port ${port} is already in use" >&2
    exit 1
  fi
done

# --- Build the two mock registries ------------------------------------------
python3 "${SCRIPT_DIR}/build_registry.py" "${ROOT}/reg"

INTERNAL_URL="http://127.0.0.1:${INTERNAL_PORT}/"
PUBLIC_URL="http://127.0.0.1:${PUBLIC_PORT}/"

python3 -m http.server "${INTERNAL_PORT}" --bind 127.0.0.1 \
  --directory "${ROOT}/reg/internal/simple" >"${ROOT}/internal.log" 2>&1 &
INTERNAL_PID=$!
python3 -m http.server "${PUBLIC_PORT}" --bind 127.0.0.1 \
  --directory "${ROOT}/reg/public_mirror/simple" >"${ROOT}/public.log" 2>&1 &
PUBLIC_PID=$!

wait_for() {
  local url="$1"
  local i=0
  while [ "${i}" -lt 50 ]; do
    if curl -s -o /dev/null -w '%{http_code}' "${url}" 2>/dev/null | grep -q '^200$'; then
      return 0
    fi
    i=$((i + 1))
    sleep 0.1
  done
  echo "ERROR: registry at ${url} never came up" >&2
  exit 1
}
wait_for "${INTERNAL_URL}"
wait_for "${PUBLIC_URL}"

# --- Helpers: run an install, then report the resolved version and whether
#     the import-time payload fired, independent of each tool's own log
#     noise (download sizes and timings are not reproducible across runs). --
installed_version() {
  local target="$1"
  local dist_info
  dist_info="$(find "${target}" -maxdepth 1 -name 'acme_internal_widgets-*.dist-info' 2>/dev/null | head -1)"
  if [ -z "${dist_info}" ]; then
    echo "none"
    return
  fi
  basename "${dist_info}" | sed -E 's/^acme_internal_widgets-(.*)\.dist-info$/\1/'
}

payload_fired() {
  local marker_dir="$1"
  if [ -f "${marker_dir}/DEPENDENCY_CONFUSION_RCE_FIRED" ]; then
    echo "yes"
  else
    echo "no"
  fi
}

run_scenario() {
  local label="$1"
  local target="$2"
  shift 2
  mkdir -p "${target}"
  local marker_dir
  marker_dir="$(mktemp -d)"
  if ! "$@" >"${ROOT}/install.log" 2>&1; then
    echo "${label}: install command failed, see ${ROOT}/install.log" >&2
    rm -rf "${marker_dir}"
    return 1
  fi
  local ver
  ver="$(installed_version "${target}")"
  # Trigger the payload exactly the way a real import would: torchtriton's
  # malicious binary "is executed when the triton package is imported"
  # (PyTorch Foundation postmortem), not when it is merely installed.
  # Not run with -I: PYTHONPATH must be honored for this import to find the
  # package in ${target}, exactly as it would for an unsandboxed victim.
  # The payload is ours, written above, and only ever writes one file under
  # the marker_dir this function created and will delete.
  DC_LAB_MARKER_DIR="${marker_dir}" PYTHONPATH="${target}" python3 -c \
    "import acme_internal_widgets" >/dev/null 2>&1 || true
  local fired
  fired="$(payload_fired "${marker_dir}")"
  # A broken import must not read as a clean "payload_fired=no": exactly one
  # of the two import markers (benign or payload) has to exist.
  if [ ! -f "${marker_dir}/INTERNAL_IMPORT_OK" ] && [ "${fired}" = "no" ]; then
    echo "${label}: import produced neither marker; the package did not import" >&2
    rm -rf "${marker_dir}"
    return 1
  fi
  rm -rf "${marker_dir}"
  echo "${label}: installed_version=${ver} payload_fired=${fired}"
}

echo "== Registries =="
echo "internal index:      ${INTERNAL_URL} (serves acme-internal-widgets 1.0.0 only)"
echo "public mirror index: ${PUBLIC_URL} (serves acme-internal-widgets 9.9.9, attacker-controlled)"
echo

echo "== Scenario A: pip, --index-url (internal) + --extra-index-url (public mirror) =="
run_scenario "A" "${ROOT}/target_a" \
  python3 -m pip install --disable-pip-version-check --no-input --no-cache-dir --no-python-version-warning \
  --index-url "${INTERNAL_URL}" --extra-index-url "${PUBLIC_URL}" \
  --target "${ROOT}/target_a" acme-internal-widgets
echo "pip has no index priority: it merges both indexes and takes the highest version number."
echo "VULNERABLE: the public mirror's 9.9.9 wins over the internal 1.0.0, and its import-time payload runs."
echo

echo "== Scenario B: pip, --index-url (internal) only, no --extra-index-url =="
run_scenario "B" "${ROOT}/target_b" \
  python3 -m pip install --disable-pip-version-check --no-input --no-cache-dir --no-python-version-warning \
  --index-url "${INTERNAL_URL}" \
  --target "${ROOT}/target_b" acme-internal-widgets
echo "SAFE: pip never contacts the public mirror, so there is nothing for an attacker to publish against."
echo

echo "== Scenario C: uv, naive pip-flag migration: --index-url (internal) + --extra-index-url (public mirror) =="
run_scenario "C" "${ROOT}/target_c" \
  uv pip install --no-cache --python "$(command -v python3)" \
  --index-url "${INTERNAL_URL}" --extra-index-url "${PUBLIC_URL}" \
  --target "${ROOT}/target_c" acme-internal-widgets
echo "uv maps the legacy --index-url to its lowest-priority 'default' index and --extra-index-url to a"
echo "higher-priority named --index, so this inverts the intended priority (uv docs, Indexes, Nov 2026)."
echo "STILL VULNERABLE: uv's first-index-strategy default does not save a lift-and-shift pip invocation."
echo

echo "== Scenario D: uv, correct priority: --index (internal) + --default-index (public mirror) =="
run_scenario "D" "${ROOT}/target_d" \
  uv pip install --no-cache --python "$(command -v python3)" \
  --index "${INTERNAL_URL}" --default-index "${PUBLIC_URL}" \
  --target "${ROOT}/target_d" acme-internal-widgets
echo "SAFE: the internal index is now the highest-priority --index entry, and uv's default"
echo "index-strategy (first-index) stops there without ever considering the public mirror's version."
