#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "$0")/.." && pwd)"
build_dir="${root_dir}/build"
data_dir="${root_dir}/demo-data"
mkdir -p "${data_dir}"
if [[ ! -x "${build_dir}/hnsw_build" ]]; then
  cmake -S "${root_dir}" -B "${build_dir}" -DENABLE_RDMA=ON -DBUILD_TESTING=ON
  cmake --build "${build_dir}" -j
fi
"${build_dir}/hnsw_build" --generate 1000 --dim 32 --M 8 --ef-construction 40 --output "${data_dir}/index.bin"
"${build_dir}/storage_server" --index "${data_dir}/index.bin" --transport mmap \
  >"${data_dir}/storage.log" 2>&1 &
storage_pid=$!
trap 'kill "${storage_pid}" 2>/dev/null || true' EXIT
sleep 0.2
"${build_dir}/compute_client" --index "${data_dir}/index.bin" --transport mmap \
  --queries-count 100 --ef-search 32 --k 10 --read-strategy fixed \
  --verbose
"${build_dir}/compute_client" --index "${data_dir}/index.bin" --transport mmap \
  --queries-count 100 --ef-search 32 --k 10 --read-strategy fixed \
  --updates-count 20 --verbose
echo "demo data: ${data_dir}"
