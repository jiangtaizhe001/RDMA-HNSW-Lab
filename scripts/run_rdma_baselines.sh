#!/usr/bin/env bash
# RDMA sweep for Baseline 1/2/3 on a 10K HNSW index.
#
# Phases (all against the same index unless overridden):
#   1. static     : REPS x 3 strategies, 1000 queries, one long-lived server.
#   2. concurrency: REPS x 3 strategies x CLIENTS concurrent clients, 500
#                   queries per client, aggregate throughput.
#   3. mixed      : REPS x 3 strategies x {erase,upsert} x ratios, with a
#                   private index copy and a fresh server per run.
#
# Every mixed run starts from an identical index copy, so the three strategies
# see exactly the same update stream and the same initial state.
#
# Environment overrides (defaults in parentheses):
#   INDEX            index path (data/index_n10000_d128_M16.bin)
#   OUT_DIR          output root (out/rdma)
#   RDMA_HOST        control-plane host (127.0.0.1)
#   RDMA_BIND        storage listen address (127.0.0.1)
#   IB_DEVICE/IB_PORT/RDMA_HOST/RDMA_BIND/PORT
#   STRATEGIES       baseline1 header_actual cached_length
#   CACHE_CAPACITY   10000
#   REPS             3
#   STATIC_QUERIES   1000
#   MIXED_QUERIES    500
#   UPDATES          300
#   RATIOS           0.1,0.3,0.5
#   CLIENTS          4
#   CONC_QUERIES     500 per client
#   EFS/K/EFC/SEED/WARMUP
#   PHASES           static concurrency mixed  (space separated)
#
# Examples:
#   scripts/run_rdma_baselines.sh
#   PHASES=static REPS=1 STATIC_QUERIES=100 scripts/run_rdma_baselines.sh
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
build="${root}/build"
out="${OUT_DIR:-${root}/out/rdma}"
data="${root}/data"
tmp="${root}/tmp"

INDEX="${INDEX:-${data}/index_n10000_d128_M16.bin}"
IB_DEVICE="${IB_DEVICE:-mlx5_0}"
IB_PORT="${IB_PORT:-1}"
HOST="${RDMA_HOST:-127.0.0.1}"
BIND="${RDMA_BIND:-127.0.0.1}"
PORT="${PORT:-18515}"

STRATEGIES="${STRATEGIES:-baseline1 header_actual cached_length}"
CACHE_CAPACITY="${CACHE_CAPACITY:-10000}"
REPS="${REPS:-3}"
STATIC_QUERIES="${STATIC_QUERIES:-1000}"
MIXED_QUERIES="${MIXED_QUERIES:-500}"
UPDATES="${UPDATES:-300}"
RATIOS="${RATIOS:-0.1,0.3,0.5}"
CLIENTS="${CLIENTS:-4}"
CONC_QUERIES="${CONC_QUERIES:-500}"
PHASES="${PHASES:-static concurrency mixed}"

EFS="${EFS:-64}"
K="${K:-10}"
EFC="${EFC:-200}"
SEED="${SEED:-42}"
WARMUP="${WARMUP:-200}"
DATASET="${DATASET:-synthetic}"

mkdir -p "${out}/logs" "${out}/per_query" "${out}/updates" \
         "${out}/concurrency/per_query" "${tmp}"

if [[ ! -x "${build}/baseline1_experiment" ]]; then
  echo "[rdma] build the project first: cmake -S . -B build -DENABLE_RDMA=ON ..."
  exit 1
fi
if [[ ! -f "${INDEX}" ]]; then
  echo "[rdma] missing index ${INDEX}; build it with hnsw_build --generate 10000"
  exit 1
fi

server_pid=""
cleanup() {
  if [[ -n "${server_pid}" ]]; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
    server_pid=""
  fi
}
trap cleanup EXIT INT TERM

strategy_key() {
  case "$1" in
    baseline1|fixed) echo "baseline1" ;;
    header_actual|header) echo "baseline2" ;;
    cached_length|cached) echo "baseline3" ;;
    *) echo "$1" ;;
  esac
}

canonical_strategy() {
  case "$1" in
    baseline1|fixed) echo "baseline1_fixed" ;;
    header_actual|header) echo "baseline2_header_first" ;;
    cached_length|cached) echo "baseline3_cached_length" ;;
    *) echo "$1" ;;
  esac
}

start_server() {
  local index="$1" log="$2"
  rm -f "${log}"
  "${build}/storage_server" --index "${index}" --transport rdma \
    --rdma-connection ib --bind "${BIND}" --port "${PORT}" \
    --ib-device "${IB_DEVICE}" --ib-port "${IB_PORT}" \
    >"${log}" 2>&1 &
  server_pid=$!
  for _ in $(seq 1 200); do
    if grep -q 'RDMA server listening' "${log}" 2>/dev/null; then
      return 0
    fi
    if ! kill -0 "${server_pid}" 2>/dev/null; then
      echo "[rdma] server exited during startup; log follows:" >&2
      cat "${log}" >&2 || true
      return 1
    fi
    sleep 0.05
  done
  echo "[rdma] server startup timeout; log follows:" >&2
  cat "${log}" >&2 || true
  return 1
}

stop_server() {
  if [[ -n "${server_pid}" ]]; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
    server_pid=""
  fi
  # Give the listener a moment before the next run reuses the port.
  sleep 0.05
}

run_driver() {
  local strategy="$1" label="$2" per_query="$3" summary="$4"
  shift 4
  "${build}/baseline1_experiment" --transport rdma --host "${HOST}" \
    --port "${PORT}" --ib-device "${IB_DEVICE}" --ib-port "${IB_PORT}" \
    --rdma-connection ib --read-strategy "${strategy}" \
    --cache-capacity "${CACHE_CAPACITY}" --ef-search "${EFS}" --k "${K}" \
    --ef-construction "${EFC}" --seed "${SEED}" --warmup "${WARMUP}" \
    --dataset "${DATASET}" --label "${label}" \
    --out "${per_query}" --summary-out "${summary}" "$@"
}

rotate_order() {
  local rep="$1"
  case $(( (rep - 1) % 3 )) in
    0) echo "baseline1 header_actual cached_length" ;;
    1) echo "cached_length header_actual baseline1" ;;
    *) echo "header_actual baseline1 cached_length" ;;
  esac
}

run_static() {
  local summary="${out}/static_summary.csv"
  rm -f "${summary}"
  echo "[rdma] phase static: reps=${REPS} queries=${STATIC_QUERIES}"
  start_server "${INDEX}" "${out}/logs/static_server.log"
  for rep in $(seq 1 "${REPS}"); do
    local label="n10000_static_r${rep}"
    for strategy in $(rotate_order "${rep}"); do
      local key
      key="$(strategy_key "${strategy}")"
      echo "[rdma] static ${label} ${strategy}"
      run_driver "${strategy}" "${label}" \
        "${out}/per_query/${label}_${key}.csv" "${summary}" \
        --queries-count "${STATIC_QUERIES}"
    done
  done
  stop_server
}

run_concurrency() {
  local summary_dir="${out}/concurrency"
  local groups="${summary_dir}/groups.csv"
  rm -f "${summary_dir}"/summary_c*.csv "${groups}"
  echo "[rdma] phase concurrency: clients=${CLIENTS} queries/client=${CONC_QUERIES}"
  start_server "${INDEX}" "${out}/logs/concurrency_server.log"
  for rep in $(seq 1 "${REPS}"); do
    local label="n10000_static4_r${rep}"
    for strategy in $(rotate_order "${rep}"); do
      local key
      key="$(strategy_key "${strategy}")"
      echo "[rdma] concurrency ${label} ${strategy} clients=${CLIENTS}"
      local start_ns
      start_ns="$(date +%s%N)"
      local pids=()
      local c
      for c in $(seq 1 "${CLIENTS}"); do
        run_driver "${strategy}" "${label}" \
          "${summary_dir}/per_query/${label}_${key}_c${c}.csv" \
          "${summary_dir}/summary_c${c}.csv" \
          --queries-count "${CONC_QUERIES}" &
        pids+=("$!")
      done
      local failed=0
      for pid in "${pids[@]}"; do
        if ! wait "${pid}"; then
          failed=1
        fi
      done
      local end_ns
      end_ns="$(date +%s%N)"
      if [[ "${failed}" -ne 0 ]]; then
        echo "[rdma] concurrency client failed" >&2
        stop_server
        return 1
      fi
      echo "${rep},$(canonical_strategy "${strategy}"),${CLIENTS},${CONC_QUERIES},$(( (end_ns - start_ns) / 1000000 ))" >>"${groups}"
    done
  done
  stop_server
  echo "rep,strategy,clients,queries_per_client,wall_ms" >"${groups}.tmp"
  cat "${groups}" >>"${groups}.tmp"
  mv "${groups}.tmp" "${groups}"
}

run_mixed() {
  local summary="${out}/mixed_summary.csv"
  rm -f "${summary}"
  echo "[rdma] phase mixed: queries=${MIXED_QUERIES} updates=${UPDATES} ratios=${RATIOS}"
  local rep op ratio strategy key label work log
  for rep in $(seq 1 "${REPS}"); do
    for op in erase upsert; do
      for ratio in ${RATIOS//,/ }; do
        for strategy in $(rotate_order "${rep}"); do
          key="$(strategy_key "${strategy}")"
          label="n10000_${op}_${ratio}_r${rep}"
          work="${tmp}/rdma_mix_${op}_${ratio}_${key}_r${rep}.bin"
          log="${out}/logs/mixed_${op}_${ratio}_${key}_r${rep}.log"
          cp -f "${INDEX}" "${work}"
          start_server "${work}" "${log}"
          echo "[rdma] mixed ${label} ${strategy}"
          run_driver "${strategy}" "${label}" \
            "${out}/per_query/${label}_${key}.csv" "${summary}" \
            --queries-count "${MIXED_QUERIES}" --updates-count "${UPDATES}" \
            --workload mixed --update-op "${op}" --update-ratio "${ratio}" \
            --update-distribution roundrobin \
            --updates-out "${out}/updates/${label}_${key}.csv"
          stop_server
          rm -f "${work}"
        done
      done
    done
  done
}

for phase in ${PHASES}; do
  case "${phase}" in
    static) run_static ;;
    concurrency) run_concurrency ;;
    mixed) run_mixed ;;
    *) echo "[rdma] unknown phase: ${phase}" >&2; exit 1 ;;
  esac
done

echo "[rdma] done. summaries:"
for f in "${out}/static_summary.csv" "${out}/mixed_summary.csv" \
         "${out}/concurrency/summary_c1.csv"; do
  if [[ -f "${f}" ]]; then
    echo "  ${f}"
  fi
done
exit 0
