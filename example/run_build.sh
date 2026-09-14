#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source example/configs/build.env
mkdir -p "$(dirname "$INDEX")"
./build/hnsw_build --generate "$COUNT" --dim "$DIM" --M "$M" \
  --ef-construction "$EF_CONSTRUCTION" --seed "$SEED" --output "$INDEX"
