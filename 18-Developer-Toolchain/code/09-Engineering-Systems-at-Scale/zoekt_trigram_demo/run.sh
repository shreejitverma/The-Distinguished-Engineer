#!/usr/bin/env bash
# Lab: a positional trigram index in the style of Zoekt, a Cox-style document trigram index,
# regex-to-query extraction, blob-hash sharding, and a toy symbol index that shows why text search is not enough.
# Pure python3 standard library, no network, deterministic output.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C
export PYTHONHASHSEED=0

python3 -I "${SCRIPT_DIR}/lab_search.py"
echo
python3 -I "${SCRIPT_DIR}/lab_semantic.py" "${SCRIPT_DIR}/semantic_fixture"
