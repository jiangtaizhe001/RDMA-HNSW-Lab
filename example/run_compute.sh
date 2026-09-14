#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source example/configs/compute-rdma.env
args=()
[[ -n "$INDEX" ]] && args+=(--index "$INDEX")
exec example/build/rdma_client "$STORAGE_HOST" "$PORT" "$IB_DEVICE" "$IB_PORT"
