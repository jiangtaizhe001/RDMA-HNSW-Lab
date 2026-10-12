#!/usr/bin/env bash
# Baseline 1 sweep (docs/baseline1-plan.md, task T7).
#
# For every requested index size it runs the static workload and then the mixed
# workloads (erase / upsert at several update ratios), appending one summary row
# per run to a single CSV.  Every mixed run works on a private copy of the index
# because mmap updates rewrite the index file in place.
#
# Environment overrides:
#   SIZES    index sizes to sweep            (default 10000,100000)
#   RATIOS   update ratios                   (default 0.1,0.3,0.5)
#   DIM M EFC EFS K QUERIES UPDATES WARMUP SEED
#   SIFT_DIR if set, use <SIFT_DIR>/sift_base_<N>.fvecs (+ offline ground truth)
#   OUT_DIR  output directory                (default <root>/out)
#
# Examples:
#   scripts/run_baseline1.sh                       # synthetic smoke sweep
#   SIZES=10000,100000,1000000 scripts/run_baseline1.sh
#   SIFT_DIR=data/sift TAG=sift scripts/run_baseline1.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
build="${root}/build"
data="${root}/data"
out="${OUT_DIR:-${root}/out}"
mkdir -p "${data}" "${out}"

SIZES="${SIZES:-10000,100000}"
RATIOS="${RATIOS:-0.1,0.3,0.5}"
DIM="${DIM:-128}"
M="${M:-16}"
EFC="${EFC:-200}"
EFS="${EFS:-64}"
K="${K:-10}"
QUERIES="${QUERIES:-1000}"
UPDATES="${UPDATES:-1000}"
WARMUP="${WARMUP:-100}"
SEED="${SEED:-42}"
SIFT_DIR="${SIFT_DIR:-}"
TAG="${TAG:-synthetic}"
SUMMARY="${out}/baseline1_summary.csv"
RESET_SUMMARY="${RESET_SUMMARY:-1}"

if [[ ! -x "${build}/baseline1_experiment" ]]; then
  echo "[baseline1] building project"
  cmake -S "${root}" -B "${build}" -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
  cmake --build "${build}" -j"$(nproc)"
fi

if [[ "${RESET_SUMMARY}" != "0" ]]; then
  rm -f "${SUMMARY}"
fi

for n in ${SIZES//,/ }; do
  gt_extra=()
  if [[ -n "${SIFT_DIR}" ]]; then
    base="${SIFT_DIR}/sift_base_${n}.fvecs"
    [[ -f "${base}" ]] || { echo "[baseline1] missing ${base}" >&2; exit 1; }
    idx="${data}/${TAG}_n${n}_d${DIM}.bin"
    if [[ ! -f "${idx}" ]]; then
      "${build}/hnsw_build" --input "${base}" --dim "${DIM}" --M "${M}" \
        --ef-construction "${EFC}" --output "${idx}"
    fi
    gt_file="${SIFT_DIR}/sift_gt_${n}_k${K}.bin"
    [[ -f "${gt_file}" ]] && gt_extra=(--gt "${gt_file}")
    query_extra=()
    [[ -f "${SIFT_DIR}/sift_query.fvecs" ]] && query_extra=(--queries "${SIFT_DIR}/sift_query.fvecs")
  else
    # Include dim/M/size in the name so different configurations never share a
    # stale synthetic index.
    idx="${data}/index_n${n}_d${DIM}_M${M}.bin"
    if [[ ! -f "${idx}" ]]; then
      "${build}/hnsw_build" --generate "${n}" --dim "${DIM}" --M "${M}" \
        --ef-construction "${EFC}" --seed "${SEED}" --output "${idx}"
    fi
    query_extra=()
  fi

  # --- static -------------------------------------------------------------
  echo "[baseline1] ${TAG} n=${n} static"
  "${build}/baseline1_experiment" --index "${idx}" --transport mmap \
    --queries-count "${QUERIES}" --ef-search "${EFS}" --k "${K}" \
    --warmup "${WARMUP}" --seed "${SEED}" \
    --dataset "${TAG}" --label "n${n}_static" --ef-construction "${EFC}" \
    "${gt_extra[@]}" "${query_extra[@]}" \
    --out "${out}/baseline1_${TAG}_n${n}_static.csv" \
    --summary-out "${SUMMARY}"

  # --- mixed: erase / upsert at several ratios ----------------------------
  for op in erase upsert; do
    for r in ${RATIOS//,/ }; do
      work="${data}/.work_${TAG}_${n}_${op}_${r}.bin"
      cp -f "${idx}" "${work}"
      echo "[baseline1] ${TAG} n=${n} ${op} ratio=${r}"
      "${build}/baseline1_experiment" --index "${work}" --transport mmap \
        --queries-count "${QUERIES}" --updates-count "${UPDATES}" \
        --ef-search "${EFS}" --k "${K}" --warmup "${WARMUP}" --seed "${SEED}" \
        --dataset "${TAG}" --label "n${n}_${op}_${r}" --ef-construction "${EFC}" \
        --workload mixed --update-op "${op}" --update-ratio "${r}" \
        --out "${out}/baseline1_${TAG}_n${n}_${op}_${r}.csv" \
        --summary-out "${SUMMARY}"
      rm -f "${work}"
    done
  done
done

echo "[baseline1] summary -> ${SUMMARY}"
