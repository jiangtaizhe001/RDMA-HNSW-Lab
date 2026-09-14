#!/usr/bin/env bash
set -euo pipefail
config_path="$(realpath "${1:-$(dirname "$0")/configs/compute-rdma.env}")"
cd "$(dirname "$0")/.."
source "$config_path"
exec example/build/rdma_client "$STORAGE_HOST" "$PORT" "${IB_DEVICE:-}" "${IB_PORT:-1}" \
  "${RDMA_CONNECTION:-ib}" "${RDMA_TIMEOUT_MS:-10000}"
