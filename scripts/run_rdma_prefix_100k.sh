#!/usr/bin/env bash
# Real-RDMA sweep for the 100K index: B1/B2/B3 plus QP G=1,2,4,8,16,32.
#
# Phases:
#   quality      precomputed-GT recall check, 1000 queries, cache 10000
#   static       main static comparison, caches 10000/100000, 5 reps
#   capacity     cache sensitivity, caches 1000/200000, 3 reps
#   mixed        serial erase/upsert, ratio 0.1/0.5, 1 rep (see plan section 9)
#   concurrency  4 independent clients, static, 1 rep
#
# Every run writes its own summary and per-query CSV; existing complete runs are
# skipped, so an interrupted sweep can be resumed without duplicate rows.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
build="${root}/build"
out="${OUT:-${root}/out/rdma_100k_prefix}"
index="${INDEX:-${root}/data/index_n100000_d128_M16.bin}"
inputs="${INPUTS:-${root}/data/prefix100k}"

timing_queries="${QUERIES:-${inputs}/query_timing_1000.fvecs}"
quality_queries="${QUALITY_QUERIES:-${inputs}/query_quality.fvecs}"
quality_gt="${QUALITY_GT:-${inputs}/gt_quality_k10.u32}"
mixed_queries="${MIXED_QUERIES_FILE:-${inputs}/query_mixed_100.fvecs}"
updates="${UPDATES_FILE:-${inputs}/updates.fvecs}"
conc_queries="${CONC_QUERIES_FILE:-${inputs}/query_conc_1000.fvecs}"

IB_DEVICE="${IB_DEVICE:-mlx5_0}"
IB_PORT="${IB_PORT:-1}"
HOST="${RDMA_HOST:-127.0.0.1}"
BIND="${RDMA_BIND:-127.0.0.1}"
PORT="${PORT:-18515}"

GS="${GS:-1 2 4 8 16 32}"
CACHES_B="${CACHES_B:-10000 100000}"
CACHES_C="${CACHES_C:-1000 200000}"
REPS_B="${REPS_B:-5}"
REPS_C="${REPS_C:-3}"
REP_FROM="${REP_FROM:-1}"
REP_TO="${REP_TO:-0}"
MIX_REPS="${MIX_REPS:-1}"
MIX_RATIOS="${MIX_RATIOS:-0.1 0.5}"
MIX_OPS="${MIX_OPS:-erase upsert}"
MIX_UPDATES="${MIX_UPDATES:-250}"
CONC_REPS="${CONC_REPS:-1}"
CLIENTS="${CLIENTS:-4}"
PHASES="${PHASES:-smoke quality static capacity mixed concurrency}"

EFS="${EFS:-64}"
K="${K:-10}"
SEED="${SEED:-42}"

if [[ ! -x "${build}/baseline1_experiment" || ! -x "${build}/storage_server" ]]; then
  echo "[100k] build first: cmake -S . -B build -DENABLE_RDMA=ON && cmake --build build" >&2
  exit 1
fi
[[ -f "${index}" ]] || { echo "[100k] missing index ${index}" >&2; exit 1; }
for f in "${timing_queries}" "${quality_queries}" "${quality_gt}" \
         "${mixed_queries}" "${updates}" "${conc_queries}"; do
  [[ -f "$f" ]] || { echo "[100k] missing input $f" >&2; exit 1; }
done

mkdir -p "${out}"/{smoke,static,capacity,quality,mixed,concurrency}/{summary,per_query,logs}
mkdir -p "${out}/mixed/updates" "${out}/tmp"

configs=(baseline1 header_actual cached_length)
for g in ${GS}; do configs+=("prefix:${g}"); done

key_for() {
  case "$1" in
    baseline1) echo baseline1 ;;
    header_actual) echo baseline2 ;;
    cached_length) echo baseline3 ;;
    prefix) echo "prefix_g$2" ;;
    *) echo "$1" ;;
  esac
}

g_for() {
  if [[ "$1" == "prefix" ]]; then echo "$2"; else echo 4; fi
}

rotate() {
  local rep="$1" count="${#configs[@]}" shift_by i
  shift_by=$(( (rep - 1) % count ))
  for ((i = 0; i < count; ++i)); do
    echo "${configs[$(((i + shift_by) % count))]}"
  done
}

server_pid=""
cleanup() {
  if [[ -n "${server_pid}" ]]; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
    server_pid=""
  fi
}
trap cleanup EXIT INT TERM

start_server() {
  local idx="$1" log="$2"
  rm -f "${log}"
  "${build}/storage_server" --index "${idx}" --transport rdma \
    --rdma-connection ib --bind "${BIND}" --port "${PORT}" \
    --ib-device "${IB_DEVICE}" --ib-port "${IB_PORT}" >"${log}" 2>&1 &
  server_pid=$!
  for _ in $(seq 1 400); do
    grep -q 'RDMA server listening' "${log}" 2>/dev/null && return 0
    kill -0 "${server_pid}" 2>/dev/null || { cat "${log}" >&2; return 1; }
    sleep 0.05
  done
  cat "${log}" >&2
  return 1
}

stop_server() {
  if [[ -n "${server_pid}" ]]; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
    server_pid=""
  fi
  sleep 0.05
}

driver() {
  local strategy="$1" g="$2" cache="$3" queries="$4" label="$5" perq="$6" summary="$7"
  shift 7
  "${build}/baseline1_experiment" --transport rdma --host "${HOST}" \
    --port "${PORT}" --ib-device "${IB_DEVICE}" --ib-port "${IB_PORT}" \
    --rdma-connection ib --index "${CURRENT_INDEX:-${index}}" \
    --queries "${queries}" --warmup 0 --ef-search "${EFS}" --k "${K}" \
    --seed "${SEED}" --dataset synthetic --label "${label}" \
    --read-strategy "${strategy}" --prefix-granularity "$(g_for "${strategy}" "${g}")" \
    --cache-capacity "${cache}" --out "${perq}" --summary-out "${summary}" "$@"
}

run_one() {
  local phase="$1" tag="$2" strategy="$3" g="$4" cache="$5" queries="$6"
  local dir="${out}/${phase}"
  local key; key="$(key_for "${strategy}" "${g}")"
  local label="n100000_${tag}"
  local summary="${dir}/summary/${label}_${key}.csv"
  local perq="${dir}/per_query/${label}_${key}.csv"
  if [[ -s "${summary}" && -s "${perq}" ]]; then
    echo "[100k] skip ${phase} ${label} ${key}"
    return 0
  fi
  echo "[100k] ${phase} ${label} ${key} cache=${cache}"
  driver "${strategy}" "${g}" "${cache}" "${queries}" "${label}" \
    "${perq}" "${summary}" >"${dir}/logs/${label}_${key}.log" 2>&1
}

run_static_like() {
  local phase="$1" caches="$2" reps="$3" queries="$4"
  start_server "${index}" "${out}/${phase}/server.log"
  local rep cache cfg strategy g
  local first="${REP_FROM}"
  local last="${REP_TO}"
  [[ "${last}" -gt 0 ]] || last="${reps}"
  for rep in $(seq "${first}" "${last}"); do
    for cache in ${caches}; do
      for cfg in $(rotate "${rep}"); do
        strategy="${cfg%%:*}"; g="${cfg##*:}"
        run_one "${phase}" "static_c${cache}_r${rep}" "${strategy}" "${g}" "${cache}" "${queries}"
      done
    done
  done
  stop_server
}

run_smoke() {
  start_server "${index}" "${out}/smoke/server.log"
  local cfg strategy g key label summary perq
  for cfg in "${configs[@]}"; do
    strategy="${cfg%%:*}"; g="${cfg##*:}"
    key="$(key_for "${strategy}" "${g}")"
    label="n100000_smoke"
    summary="${out}/smoke/summary/${label}_${key}.csv"
    perq="${out}/smoke/per_query/${label}_${key}.csv"
    if [[ -s "${summary}" && -s "${perq}" ]]; then
      echo "[100k] skip smoke ${label} ${key}"
      continue
    fi
    echo "[100k] smoke ${label} ${key}"
    driver "${strategy}" "${g}" 10000 "${mixed_queries}" "${label}" \
      "${perq}" "${summary}" >"${out}/smoke/logs/${label}_${key}.log" 2>&1
  done
  stop_server
}

run_quality() {
  start_server "${index}" "${out}/quality/server.log"
  local cfg strategy g
  for cfg in "${configs[@]}"; do
    strategy="${cfg%%:*}"; g="${cfg##*:}"
    local key label summary perq
    key="$(key_for "${strategy}" "${g}")"
    label="n100000_quality"
    summary="${out}/quality/summary/${label}_${key}.csv"
    perq="${out}/quality/per_query/${label}_${key}.csv"
    if [[ -s "${summary}" && -s "${perq}" ]]; then
      echo "[100k] skip quality ${label} ${key}"
      continue
    fi
    echo "[100k] quality ${label} ${key}"
    driver "${strategy}" "${g}" 10000 "${quality_queries}" "${label}" \
      "${perq}" "${summary}" --gt "${quality_gt}" \
      >"${out}/quality/logs/${label}_${key}.log" 2>&1
  done
  stop_server
}

run_mixed() {
  local rep op ratio cfg strategy g key label work
  for rep in $(seq 1 "${MIX_REPS}"); do
    for op in ${MIX_OPS}; do
      for ratio in ${MIX_RATIOS}; do
        for cfg in $(rotate "${rep}"); do
          strategy="${cfg%%:*}"; g="${cfg##*:}"
          key="$(key_for "${strategy}" "${g}")"
          label="n100000_${op}_${ratio}_r${rep}"
          local summary="${out}/mixed/summary/${label}_${key}.csv"
          local perq="${out}/mixed/per_query/${label}_${key}.csv"
          if [[ -s "${summary}" && -s "${perq}" ]]; then
            echo "[100k] skip mixed ${label} ${key}"
            continue
          fi
          work="${out}/tmp/mix_${op}_${ratio}_${key}_r${rep}.bin"
          cp -f "${index}" "${work}"
          start_server "${work}" "${out}/mixed/logs/${label}_${key}_server.log"
          echo "[100k] mixed ${label} ${key}"
          CURRENT_INDEX="${work}" driver "${strategy}" "${g}" 10000 \
            "${mixed_queries}" "${label}" "${perq}" "${summary}" \
            --workload mixed --update-op "${op}" --update-ratio "${ratio}" \
            --updates "${updates}" --updates-count "${MIX_UPDATES}" \
            --update-distribution roundrobin \
            --updates-out "${out}/mixed/updates/${label}_${key}.csv" \
            >"${out}/mixed/logs/${label}_${key}.log" 2>&1
          stop_server
          rm -f "${work}"
        done
      done
    done
  done
}

run_concurrency() {
  local groups="${out}/concurrency/groups.csv"
  [[ -f "${groups}" ]] || echo "rep,strategy,clients,queries_per_client,wall_ms" >"${groups}"
  start_server "${index}" "${out}/concurrency/server.log"
  local rep cfg strategy g key label c start_ns end_ns
  for rep in $(seq 1 "${CONC_REPS}"); do
    for cfg in "${configs[@]}"; do
      strategy="${cfg%%:*}"; g="${cfg##*:}"
      key="$(key_for "${strategy}" "${g}")"
      label="n100000_conc_r${rep}_${key}_c"
      echo "[100k] concurrency ${label} clients=${CLIENTS}"
      start_ns="$(date +%s%N)"
      local pids=()
      for c in $(seq 1 "${CLIENTS}"); do
        local summary="${out}/concurrency/summary/${label}${c}.csv"
        local perq="${out}/concurrency/per_query/${label}${c}.csv"
        if [[ -s "${summary}" && -s "${perq}" ]]; then
          continue
        fi
        driver "${strategy}" "${g}" 10000 "${conc_queries}" "${label}${c}" \
          "${perq}" "${summary}" >"${out}/concurrency/logs/${label}${c}.log" 2>&1 &
        pids+=("$!")
      done
      for pid in "${pids[@]:-}"; do [[ -n "${pid}" ]] && wait "${pid}"; done
      end_ns="$(date +%s%N)"
      echo "${rep},${key},${CLIENTS},1000,$(( (end_ns - start_ns) / 1000000 ))" >>"${groups}"
    done
  done
  stop_server
}

merge_summaries() {
  local src="$1" dst="$2"
  local files=()
  while IFS= read -r f; do files+=("$f"); done < <(find "${src}" -maxdepth 1 -name '*.csv' -type f | sort)
  [[ ${#files[@]} -gt 0 ]] || return 0
  head -1 "${files[0]}" >"${dst}"
  local f
  for f in "${files[@]}"; do tail -n +2 "$f" >>"${dst}"; done
}

for phase in ${PHASES}; do
  case "${phase}" in
    smoke) run_smoke ;;
    quality) run_quality ;;
    static) run_static_like static "${CACHES_B}" "${REPS_B}" "${timing_queries}" ;;
    capacity) run_static_like capacity "${CACHES_C}" "${REPS_C}" "${timing_queries}" ;;
    mixed) run_mixed ;;
    concurrency) run_concurrency ;;
    *) echo "[100k] unknown phase: ${phase}" >&2; exit 1 ;;
  esac
done

merge_summaries "${out}/static/summary" "${out}/static_summary.csv"
merge_summaries "${out}/capacity/summary" "${out}/capacity_summary.csv"
merge_summaries "${out}/quality/summary" "${out}/quality_summary.csv"
merge_summaries "${out}/mixed/summary" "${out}/mixed_summary.csv"
# The shared analyzer expects static + capacity grouped by cache in one table.
if [[ -f "${out}/static_summary.csv" && -f "${out}/capacity_summary.csv" ]]; then
  head -1 "${out}/static_summary.csv" >"${out}/static_all_summary.csv"
  tail -n +2 "${out}/static_summary.csv" >>"${out}/static_all_summary.csv"
  tail -n +2 "${out}/capacity_summary.csv" >>"${out}/static_all_summary.csv"
  mv "${out}/static_all_summary.csv" "${out}/static_summary.csv"
fi
echo "[100k] done: ${out}"
