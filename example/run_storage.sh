#!/usr/bin/env bash
set -euo pipefail
config_path="$(realpath "${1:-$(dirname "$0")/configs/storage-rdma.env}")"
cd "$(dirname "$0")/.."
source "$config_path"
exec ./build/storage_server --index "$INDEX" --transport rdma --bind "$BIND" \
  --port "$PORT" --ib-device "${IB_DEVICE:-}" --ib-port "${IB_PORT:-1}" \
  --rdma-connection "${RDMA_CONNECTION:-ib}" --rdma-timeout-ms "${RDMA_TIMEOUT_MS:-10000}"
