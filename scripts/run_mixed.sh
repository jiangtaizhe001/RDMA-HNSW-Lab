#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="${root_dir}/build"
index="${1:-${root_dir}/demo-data/index.bin}"
if [[ ! -f "$index" ]]; then
  mkdir -p "$(dirname "$index")"
  "${build_dir}/hnsw_build" --generate 2000 --dim 32 --M 8 --ef-construction 40 --output "$index"
fi
"${build_dir}/compute_client" --index "$index" --transport mmap --workload mixed \
  --queries-count 500 --updates-count 100 --update-ratio 0.2 \
  --ef-search 32 --k 10 --read-strategy fixed
