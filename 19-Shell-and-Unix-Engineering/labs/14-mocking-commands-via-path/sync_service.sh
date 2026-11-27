#!/bin/sh
# sync_service.sh: synchronize artifacts to a remote endpoint.
# Relies on external utilities: curl, date, logger.
set -eu

ENDPOINT="${1:-}"
PAYLOAD="${2:-}"

if [ -z "$ENDPOINT" ]; then
  echo "sync_service: missing endpoint" >&2
  exit 1
fi

if [ -z "$PAYLOAD" ]; then
  echo "sync_service: missing payload" >&2
  exit 1
fi

timestamp=$(date +%s)

if ! response=$(curl -s -S --fail --data-binary "$PAYLOAD" "$ENDPOINT" 2>&1); then
  logger -p user.err "sync_service: upload failed to $ENDPOINT" 2>/dev/null || true
  echo "sync_service: upload failed: $response" >&2
  exit 2
fi

logger -p user.info "sync_service: upload succeeded at $timestamp" 2>/dev/null || true
printf 'SUCCESS: %s (timestamp: %s)\n' "$response" "$timestamp"
