#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source example/configs/storage-rdma.env
exec ./build/storage_server --index "$INDEX" --transport rdma --bind "$BIND" \
  --port "$PORT" --ib-device "$IB_DEVICE" --ib-port "$IB_PORT"
