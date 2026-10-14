#!/usr/bin/env bash
# Baseline 2 (header-first) sweep (docs/baseline2-plan.md, tasks T4/T7/T8).
#
# Runs Baseline 1 (fixed) and Baseline 2 (header_actual) over the *same* configs
# and the *same* workload, appending one summary row per run to a single CSV.
# Both strategies are produced by the same driver with the same query set and
# the same starting index, so their rows pair up on (dataset, label); the
# analysis script then computes the per-config deltas (saved bytes, extra ops,
# latency change, rtt per dependent read).
#
# Every mixed run works on a private copy of the index because mmap updates
# rewrite the index file in place.  Both strategies start from an identical
# copy and the driver derives the update sequence deterministically from the
# seed, so the two strategies see exactly the same update stream.
#
# Environment overrides:
#   CONFIGS  space separated size:dim:M list
#            (default: 10000:128:16 100000:128:16 10000:32:16 10000:128:8 10000:128:32)
#   RATIOS   update ratios                   (default 0.1,0.3,0.5)
#   QUERIES UPDATES WARMUP EFS K EFC SEED
#   OUT_DIR  output directory                (default <root>/out)
#   RESET_SUMMARY=0 to append instead of truncating the summary CSV
#
# Examples:
#   scripts/run_baseline2.sh
#   CONFIGS="10000:128:16" QUERIES=200 scripts/run_baseline2.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
build="${root}/build"
data="${root}/data"
out="${OUT_DIR:-${root}/out}"
mkdir -p "${data}" "${out}"

CONFIGS="${CONFIGS:-10000:128:16 100000:128:16 10000:32:16 10000:128:8 10000:128:32}"
RATIOS="${RATIOS:-0.1,0.3,0.5}"
EFS="${EFS:-64}"
K="${K:-10}"
EFC="${EFC:-200}"
QUERIES="${QUERIES:-500}"
UPDATES="${UPDATES:-500}"
WARMUP="${WARMUP:-50}"
SEED="${SEED:-42}"
DATASET="${DATASET:-synthetic}"
SUMMARY="${out}/baseline2_summary.csv"
RESET_SUMMARY="${RESET_SUMMARY:-1}"

if [[ ! -x "${build}/baseline1_experiment" ]]; then
  echo "[baseline2] building project"
  cmake -S "${root}" -B "${build}" -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
  cmake --build "${build}" -j"$(nproc)"
fi

if [[ "${RESET_SUMMARY}" != "0" ]]; then
  rm -f "${SUMMARY}"
fi

# run <strategy> <index> <label> [extra driver args...]
run() {
  local strat="$1" idx="$2" label="$3"
  shift 3
  "${build}/baseline1_experiment" --index "${idx}" --transport mmap \
    --queries-count "${QUERIES}" --ef-search "${EFS}" --k "${K}" \
    --warmup "${WARMUP}" --seed "${SEED}" --dataset "${DATASET}" \
    --label "${label}" --ef-construction "${EFC}" \
    --read-strategy "${strat}" \
    --out "${out}/baseline2_${label}_${strat}.csv" \
    --summary-out "${SUMMARY}" "$@"
}

for cfg in ${CONFIGS}; do
  n="${cfg%%:*}"
  rest="${cfg#*:}"
  dim="${rest%%:*}"
  M="${rest##*:}"
  idx="${data}/index_n${n}_d${dim}_M${M}.bin"
  if [[ ! -f "${idx}" ]]; then
    echo "[baseline2] building ${idx}"
    "${build}/hnsw_build" --generate "${n}" --dim "${dim}" --M "${M}" \
      --ef-construction "${EFC}" --seed "${SEED}" --output "${idx}"
  fi
  tag="n${n}_d${dim}_M${M}"

  for strat in baseline1 header_actual; do
    echo "[baseline2] ${tag} static ${strat}"
    run "${strat}" "${idx}" "${tag}_static"

    for op in erase upsert; do
      for r in ${RATIOS//,/ }; do
        work="${data}/.b2work_${tag}_${op}_${r}.bin"
        cp -f "${idx}" "${work}"
        echo "[baseline2] ${tag} ${op} ratio=${r} ${strat}"
        run "${strat}" "${work}" "${tag}_${op}_${r}" \
          --workload mixed --update-op "${op}" --update-ratio "${r}" \
          --updates-count "${UPDATES}"
        rm -f "${work}"
      done
    done
  done
done

echo "[baseline2] summary -> ${SUMMARY}"
