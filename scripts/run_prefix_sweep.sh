#!/usr/bin/env bash
# Local (mmap) G-sweep for the quantized-prefix strategy.
#
# This is the cheap, reproducible "10K development set" experiment described in
# docs/prefix-read-design.md section 6.3.  It is NOT an RDMA timing run: mmap
# removes the transport from the latency path, so only READ/byte counts and
# cache behaviour should be compared across strategies here.
#
# Layout (default OUT=out/prefix_sweep):
#   static_summary.csv          one row per (cache, rep, config)
#   mixed_summary.csv           one row per (upsert ratio, rep, config)
#   per_query/<label>_<key>.csv per-query samples, warm-up NOT skipped
#   logs/                       driver stdout/stderr
#
# Every run starts with an empty cache and `--warmup 0`, so the analysis script
# can report the cold window (first COLD_SPLIT queries) and the steady window
# (the rest) from the same trace.
#
# Environment overrides (defaults in parentheses):
#   INDEX        (data/index_n10000_d128_M16.bin)
#   OUT          (out/prefix_sweep)
#   CACHES       (100 1000 10000 100000)
#   GS           (1 2 4 8 16 32)
#   REPS         (3)
#   QUERIES      (1000)
#   COLD_SPLIT   (200)
#   MIXED        (1=on)
#   MIXED_QUERIES (300)  UPDATES (100)  RATIOS (0.1 0.3 0.5)
#   MIXED_GS     (1 2 4 8 16)  MIXED_CACHE (10000)  MIXED_REPS (2)
#   EFS (64)  K (10)  SEED (42)
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
build="${root}/build"
index="${INDEX:-${root}/data/index_n10000_d128_M16.bin}"
out="${OUT:-${root}/out/prefix_sweep}"

caches="${CACHES:-100 1000 10000 100000}"
gs="${GS:-1 2 4 8 16 32}"
reps="${REPS:-3}"
queries="${QUERIES:-1000}"

mixed="${MIXED:-1}"
static_phase="${STATIC:-1}"
reset="${RESET:-1}"
mixed_queries="${MIXED_QUERIES:-300}"
updates="${UPDATES:-100}"
ratios="${RATIOS:-0.1 0.3 0.5}"
mixed_gs="${MIXED_GS:-1 2 4 8 16}"
mixed_cache="${MIXED_CACHE:-10000}"
mixed_reps="${MIXED_REPS:-2}"

efs="${EFS:-64}"
k="${K:-10}"
seed="${SEED:-42}"

if [[ ! -x "${build}/baseline1_experiment" ]]; then
  echo "[prefix-sweep] build first: cmake -S . -B build && cmake --build build" >&2
  exit 1
fi
if [[ ! -f "${index}" ]]; then
  echo "[prefix-sweep] missing index ${index}; run hnsw_build --generate 10000" >&2
  exit 1
fi

if [[ "${reset}" == "1" ]]; then
  rm -rf "${out}"
fi
mkdir -p "${out}/per_query" "${out}/logs" "${out}/tmp"

configs=(baseline1:0 header_actual:0 cached_length:0)
for g in ${gs}; do
  configs+=("prefix:${g}")
done

config_key() {
  case "$1" in
    baseline1) echo "baseline1" ;;
    header_actual) echo "baseline2" ;;
    cached_length) echo "baseline3" ;;
    prefix) echo "prefix_g$2" ;;
    *) echo "$1" ;;
  esac
}

rotate() {
  local rep="$1" count="${#configs[@]}" shift_by i
  shift_by=$(( (rep - 1) % count ))
  for ((i = 0; i < count; ++i)); do
    echo "${configs[$(((i + shift_by) % count))]}"
  done
}

run_static() {
  local label="$1" strategy="$2" granularity="$3" cache="$4" per_query="$5"
  local granularity_arg="${granularity}"
  [[ "${strategy}" == "prefix" ]] || granularity_arg=4
  "${build}/baseline1_experiment" --transport mmap --index "${index}" \
    --queries-count "${queries}" --warmup 0 \
    --ef-search "${efs}" --k "${k}" --seed "${seed}" \
    --dataset synthetic --label "${label}" \
    --read-strategy "${strategy}" --prefix-granularity "${granularity_arg}" \
    --cache-capacity "${cache}" \
    --out "${per_query}" --summary-out "${out}/static_summary.csv" \
    >"${out}/logs/${label}_$(config_key "${strategy}" "${granularity}").log" 2>&1
}

echo "[prefix-sweep] static: ${reps} reps x caches=${caches}"
if [[ "${static_phase}" == "1" ]]; then
  for rep in $(seq 1 "${reps}"); do
    for cache in ${caches}; do
      for cfg in $(rotate "${rep}"); do
        strategy="${cfg%%:*}"
        granularity="${cfg##*:}"
        key="$(config_key "${strategy}" "${granularity}")"
        label="n10000_static_c${cache}_r${rep}"
        echo "[prefix-sweep] static ${label} ${key}"
        run_static "${label}" "${strategy}" "${granularity}" "${cache}" \
          "${out}/per_query/${label}_${key}.csv"
      done
    done
  done
fi

if [[ "${mixed}" == "1" ]]; then
  pristine="${out}/tmp/pristine.bin"
  cp -f "${index}" "${pristine}"
  mixed_configs=(baseline1:0 cached_length:0)
  for g in ${mixed_gs}; do
    mixed_configs+=("prefix:${g}")
  done
  echo "[prefix-sweep] mixed: ${mixed_reps} reps x ratios=${ratios}"
  for rep in $(seq 1 "${mixed_reps}"); do
    for ratio in ${ratios}; do
      count="${#mixed_configs[@]}"
      shift_by=$(( (rep - 1) % count ))
      for ((i = 0; i < count; ++i)); do
        cfg="${mixed_configs[$(((i + shift_by) % count))]}"
        strategy="${cfg%%:*}"
        granularity="${cfg##*:}"
        key="$(config_key "${strategy}" "${granularity}")"
        label="n10000_upsert_${ratio}_c${mixed_cache}_r${rep}"
        work="${out}/tmp/mix_${ratio}_${key}.bin"
        cp -f "${pristine}" "${work}"
        granularity_arg="${granularity}"
        [[ "${strategy}" == "prefix" ]] || granularity_arg=4
        echo "[prefix-sweep] mixed ${label} ${key}"
        "${build}/baseline1_experiment" --transport mmap --index "${work}" \
          --queries-count "${mixed_queries}" --warmup 0 \
          --ef-search "${efs}" --k "${k}" --seed "${seed}" \
          --dataset synthetic --label "${label}" \
          --read-strategy "${strategy}" --prefix-granularity "${granularity_arg}" \
          --cache-capacity "${mixed_cache}" --workload mixed \
          --update-op upsert --update-ratio "${ratio}" \
          --updates-count "${updates}" --update-distribution roundrobin \
          --out "${out}/per_query/${label}_${key}.csv" \
          --summary-out "${out}/mixed_summary.csv" \
          >"${out}/logs/${label}_${key}.log" 2>&1
        rm -f "${work}"
      done
    done
  done
  rm -f "${pristine}"
fi

echo "[prefix-sweep] done: ${out}"
