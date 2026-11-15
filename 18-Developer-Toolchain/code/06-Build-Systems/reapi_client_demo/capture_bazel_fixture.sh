#!/usr/bin/env bash
# requires: network
#
# Regenerates fixtures/bazel/ from a real Bazel build; run.sh never calls this script.
# It needs Bazel (bazelisk) and, on a cold repository cache, network access to the Bazel
# Central Registry, which is why run.sh replays the captured files instead.
#
# Usage: bash capture_bazel_fixture.sh [output_user_root]
# The optional output_user_root lets a warm Bazel install and repository cache be reused.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
OUR="${1:-${WORK}/our}"
OUT="${SCRIPT_DIR}/fixtures/bazel"
BAZEL_VERSION=9.3.2

export LC_ALL=C
WS="${WORK}/ws"
DC="${WORK}/disk_cache"
mkdir -p "${WS}/pkg"
cd "${WS}"
echo "${BAZEL_VERSION}" > .bazelversion
: > MODULE.bazel
printf 'hello reapi\n' > pkg/in.txt
cat > pkg/BUILD <<'EOF'
genrule(
    name = "up",
    srcs = ["in.txt"],
    outs = ["up.txt"],
    cmd = "tr a-z A-Z < $< > $@",
)
EOF

bz() {
    bazel --output_user_root="${OUR}" --nohome_rc --nosystem_rc "$@"
}

build() { # build <variant> [extra flags...]
    local variant="$1"
    shift
    bz build --noenable_workspace --disk_cache="${DC}" \
        --execution_log_json_file="${WORK}/${variant}.json" "$@" //pkg:up >/dev/null 2>&1
}

build base
build env --action_env=REAPI_DEMO=on
printf 'hello reapi v2\n' > pkg/in.txt
build input
printf 'hello reapi\n' > pkg/in.txt
bz clean >/dev/null 2>&1
build hit

rm -rf "${OUT}"
mkdir -p "${OUT}"
python3 - "${WORK}" "${DC}" "${OUT}" "$(bazel --version)" "$(uname -s)-$(uname -m)" <<'EOF'
import json, os, shutil, sys

work, dc, out, version, host = sys.argv[1:]
keep = ("commandArgs", "environmentVariables", "inputs", "listedOutputs", "actualOutputs",
        "runner", "cacheHit", "remoteCacheable", "mnemonic", "digest", "exitCode")
manifest = {"bazel": version, "host": host, "variants": {}}
for variant in ("base", "env", "input", "hit"):
    text = open(os.path.join(work, variant + ".json")).read()
    dec, i, spawns = json.JSONDecoder(), 0, []
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i < len(text):
            obj, i = dec.raw_decode(text, i)
            spawns.append(obj)
    (spawn,) = [s for s in spawns if s.get("mnemonic") == "Genrule"]
    rec = {k: spawn[k] for k in keep if k in spawn}
    for inp in rec["inputs"]:
        inp.pop("symlinkTargetPath", None)
    manifest["variants"][variant] = rec
    action_hash = rec["digest"]["hash"]
    vdir = os.path.join(out, variant)
    os.makedirs(vdir)
    # The disk cache keeps the ActionResult under ac/ and the Action, Command and outputs under cas/.
    shutil.copyfile(os.path.join(dc, "ac", action_hash[:2], action_hash), os.path.join(vdir, "action_result.bin"))
    shutil.copyfile(os.path.join(dc, "cas", action_hash[:2], action_hash), os.path.join(vdir, "action.bin"))
listing = sorted(os.path.relpath(os.path.join(r, f), dc) for r, _, fs in os.walk(dc) for f in fs)
manifest["disk_cache_files"] = listing
cas = {}
for rel in listing:
    if rel.startswith("cas" + os.sep):
        name = rel.split(os.sep)[-1]
        cas[name] = open(os.path.join(dc, rel), "rb").read().hex()
manifest["cas_blobs_hex"] = cas
with open(os.path.join(out, "manifest.json"), "w") as f:
    json.dump(manifest, f, indent=1, sort_keys=True)
    f.write("\n")
EOF
bz shutdown >/dev/null 2>&1 || true
echo "wrote ${OUT}"
