#!/bin/bash
set -euo pipefail

LOG_FILE="${1:-worker.log}"

# A graceful shutdown takes time (flushing, deregistering), which gives the
# lab a deterministic window in which a second signal can arrive.
cleanup() {
    sleep 0.5
    exit 0
}

trap cleanup TERM INT

while true; do
    echo "heartbeat" >> "$LOG_FILE" 2>/dev/null || true
    sleep 0.2
done
