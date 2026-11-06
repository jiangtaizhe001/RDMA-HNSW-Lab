#!/usr/bin/env bash
# Baseline 3 (compute-side neighbor-length cache) sweep.
# See docs/baseline3-plan.md T5/T6/T9 and section 9.
#
# For every configuration the script runs all three strategies
#   baseline1      (fixed: read the whole capacity)
#   header_actual  (Baseline 2: header trip, then exactly degree slots)
#   cached_length  (Baseline 3: predict the degree, then validate it in-place)
# over the *same* query set and the *same* update stream, so their rows pair up
# on (dataset, label) inside one summary CSV.
#
# Extra Baseline 3 dimensions:
#   * CACHE_CAPACITIES - the number of cached (node, level) entries.  Keep this
#     close to the index size: a 10K index only has ~10.7K keys, so the default
#     capacity of 100000 would never evict anything (baseline3-plan 0-5).
#   * DISTS - update id distribution (roundrobin|random|hot).  The same
#     distribution is used for all three strategies.
#
# Every mixed run works on a private copy of the index because mmap updates
# rewrite the index file in place; that keeps the three strategies comparable.
#
# Environment overrides:
#   CONFIGS   space separated size:dim:M list (default 10000:128:16)
#   RATIOS    update ratios                 (default 0.1,0.3,0.5)
#   CACHE_CAPACITIES comma separated sizes  (default 100,1000,10000,100000)
#   DISTS     update distributions          (default roundrobin hot)
#   HOT_SIZE  hot-set size for 'hot'        (default driver: node_count/100)
#   QUERIES UPDATES WARMUP EFS K EFC SEED DATASET
#   RECALL_DIR  optional directory with <prefix>_base_<n>.fvecs and, optionally,
#               <prefix>_query.fvecs / <prefix>_gt_<n>_k<k>.bin.  When set, the
#               index is built from the base file (so neighbour ids match the
#               ground-truth ids) and recall is actually measured.
#   RECALL_PREFIX  file name prefix inside RECALL_DIR (default sift)
#   OUT_DIR   output directory              (default <root>/out)
#   RESET_SUMMARY=0 to append instead of truncating the summary CSV
#   SKIP_B1B2=1 to only run Baseline 3 (rows then have no B1/B2 partner)
#
# Examples:
#   scripts/run_baseline3.sh
#   CONFIGS="10000:128:16" QUERIES=200 UPDATES=100 CACHE_CAPACITIES=1000 \
#     RATIOS=0.3 DISTS=roundrobin scripts/run_baseline3.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
build="${root}/build"
data="${root}/data"
tmp="${root}/tmp"
out="${OUT_DIR:-${root}/out}"
mkdir -p "${data}" "${tmp}" "${out}"

CONFIGS="${CONFIGS:-10000:128:16}"
RATIOS="${RATIOS:-0.1,0.3,0.5}"
CACHE_CAPACITIES="${CACHE_CAPACITIES:-100,1000,10000,100000}"
DISTS="${DISTS:-roundrobin hot}"
HOT_SIZE="${HOT_SIZE:-}"
EFS="${EFS:-64}"
K="${K:-10}"
EFC="${EFC:-200}"
QUERIES="${QUERIES:-500}"
UPDATES="${UPDATES:-500}"
WARMUP="${WARMUP:-50}"
SEED="${SEED:-42}"
DATASET="${DATASET:-synthetic}"
RECALL_DIR="${RECALL_DIR:-}"
RECALL_PREFIX="${RECALL_PREFIX:-sift}"
recall_args=()
SUMMARY="${out}/baseline3_summary.csv"
RESET_SUMMARY="${RESET_SUMMARY:-1}"
SKIP_B1B2="${SKIP_B1B2:-0}"

if [[ ! -x "${build}/baseline1_experiment" ]]; then
  echo "[baseline3] building project"
  cmake -S "${root}" -B "${build}" -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
  cmake --build "${build}" -j"$(nproc)"
fi

if [[ "${RESET_SUMMARY}" != "0" ]]; then
  rm -f "${SUMMARY}"
fi

# Optional hot-set size (amplifies degree drift for 'hot'; see report 7.2).
hot_args=()
if [[ -n "${HOT_SIZE}" ]]; then
  hot_args=(--update-hot-size "${HOT_SIZE}")
fi

# run <strategy> <index> <label> <per-query suffix> [extra driver args...]
run() {
  local strat="$1" idx="$2" label="$3" suffix="$4"
  shift 4
  "${build}/baseline1_experiment" --index "${idx}" --transport mmap \
    --queries-count "${QUERIES}" --ef-search "${EFS}" --k "${K}" \
    --warmup "${WARMUP}" --seed "${SEED}" --dataset "${DATASET}" \
    --label "${label}" --ef-construction "${EFC}" \
    --read-strategy "${strat}" \
    --out "${out}/baseline3_${label}_${strat}${suffix}.csv" \
    --summary-out "${SUMMARY}" "${recall_args[@]}" "$@"
}

for cfg in ${CONFIGS}; do
  n="${cfg%%:*}"
  rest="${cfg#*:}"
  dim="${rest%%:*}"
  M="${rest##*:}"
  recall_args=()
  idx="${data}/index_n${n}_d${dim}_M${M}.bin"
  if [[ -n "${RECALL_DIR}" ]]; then
    base="${RECALL_DIR}/${RECALL_PREFIX}_base_${n}.fvecs"
    [[ -f "${base}" ]] || { echo "[baseline3] missing ${base}" >&2; exit 1; }
    gt="${RECALL_DIR}/${RECALL_PREFIX}_gt_${n}_k${K}.bin"
    query_file="${RECALL_DIR}/${RECALL_PREFIX}_query.fvecs"
    if [[ -f "${gt}" && ! -f "${query_file}" ]]; then
      echo "[baseline3] ground truth requires matching queries: ${query_file}" >&2
      exit 1
    fi
    if [[ -f "${gt}" ]]; then
      recall_args=(--gt "${gt}")
    else
      recall_args=(--base "${base}")
    fi
    [[ -f "${RECALL_DIR}/${RECALL_PREFIX}_query.fvecs" ]] && \
      recall_args+=(--queries "${RECALL_DIR}/${RECALL_PREFIX}_query.fvecs")
    # Build from the recall base so neighbour ids equal ground-truth rows.
    base_hash="$(sha256sum -- "${base}")"
    base_hash="${base_hash%% *}"
    mkdir -p "${data}/recall_indexes"
    idx="${data}/recall_indexes/base_${base_hash}_n${n}_d${dim}_M${M}_efc${EFC}_seed${SEED}.bin"
    if [[ ! -f "${idx}" ]]; then
      echo "[baseline3] building ${idx} from ${base}"
      "${build}/hnsw_build" --input "${base}" --dim "${dim}" --M "${M}" \
        --ef-construction "${EFC}" --seed "${SEED}" --output "${idx}"
    fi
  elif [[ ! -f "${idx}" ]]; then
    echo "[baseline3] building ${idx}"
    "${build}/hnsw_build" --generate "${n}" --dim "${dim}" --M "${M}" \
      --ef-construction "${EFC}" --seed "${SEED}" --output "${idx}"
  fi
  tag="n${n}_d${dim}_M${M}"

  # ---- static workload --------------------------------------------------
  label="${tag}_static"
  if [[ "${SKIP_B1B2}" != "1" ]]; then
    for strat in baseline1 header_actual; do
      echo "[baseline3] ${label} ${strat}"
      run "${strat}" "${idx}" "${label}" ""
    done
  fi
  for cap in ${CACHE_CAPACITIES//,/ }; do
    echo "[baseline3] ${label} cached_length cap=${cap}"
    run cached_length "${idx}" "${label}" "_c${cap}" --cache-capacity "${cap}"
  done

  # Mixed workloads mutate the index, so the static ground truth no longer
  # describes the current data; leave recall unmeasured there (as Baseline 1).
  recall_args=()
  # ---- mixed workloads --------------------------------------------------
  for op in erase upsert; do
    for r in ${RATIOS//,/ }; do
      for dist in ${DISTS}; do
        label="${tag}_${op}_${r}_${dist}"
        for strat in baseline1 header_actual cached_length; do
          if [[ "${SKIP_B1B2}" == "1" && "${strat}" != "cached_length" ]]; then
            continue
          fi
          work="${tmp}/b3work_${tag}_${op}_${r}_${dist}_${strat}.bin"
          cp -f "${idx}" "${work}"
          if [[ "${strat}" == "cached_length" ]]; then
            for cap in ${CACHE_CAPACITIES//,/ }; do
              echo "[baseline3] ${label} ${strat} cap=${cap}"
              run "${strat}" "${work}" "${label}" "_c${cap}" \
                --workload mixed --update-op "${op}" --update-ratio "${r}" \
                --updates-count "${UPDATES}" --update-distribution "${dist}" \
                --cache-capacity "${cap}" "${hot_args[@]}"
            done
          else
            echo "[baseline3] ${label} ${strat}"
            run "${strat}" "${work}" "${label}" "" \
              --workload mixed --update-op "${op}" --update-ratio "${r}" \
              --updates-count "${UPDATES}" --update-distribution "${dist}" \
              "${hot_args[@]}"
          fi
          rm -f "${work}"
        done
      done
    done
  done
done

echo "[baseline3] summary -> ${SUMMARY}"
