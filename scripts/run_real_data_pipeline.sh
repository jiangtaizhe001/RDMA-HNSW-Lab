#!/usr/bin/env bash
# Unattended, resumable P0/P1 pipeline for docs/real-data-rdma-improvement-plan.md.
#
# Sequence per dataset: prepare (subsets + query split + exact GT) -> build one
# index per scale (M=16, ef_construction=200, seed=42) -> calibrate the 0.95 and
# 0.99 expansion budgets on the calibration split -> RDMA quality+static matrix.
# For SIFT the pre-registered cache-capacity scan runs before the main matrix.
#
# Every step is idempotent: a step is skipped when its `.done` marker exists, a
# campaign reuses completed runs by resume key, and failures are recorded in
# out/real_data_rdma/status/ without stopping the remaining steps.
#
# Progress:   tail -f out/real_data_rdma/logs/*.log
# Status:     ls out/real_data_rdma/status/
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
out="$root/out/real_data_rdma"
logs="$out/logs"
status="$out/status"
mkdir -p "$logs" "$status"
PY="${PY:-python3}"
SIZES="10000,100000,1000000"
REPS="${REPS:-5}"

say() { echo "[$(date -Is)] $*"; }

step() {
  local name="$1"; shift
  if [[ -f "$status/$name.done" ]]; then
    say "skip $name (done)"
    return 0
  fi
  say "START $name"
  local started rc
  started=$(date +%s)
  "$@" >>"$logs/$name.log" 2>&1
  rc=$?
  if [[ $rc -eq 0 ]]; then
    rm -f "$status/$name.failed"
    echo "ok seconds=$(( $(date +%s) - started )) $(date -Is)" >"$status/$name.done"
    say "DONE $name"
    return 0
  fi
  echo "failed rc=$rc seconds=$(( $(date +%s) - started )) $(date -Is)" \
    >"$status/$name.failed"
  say "FAILED $name rc=$rc"
  return "$rc"
}

completed_runs() { find "$1/runs" -name completed.json 2>/dev/null | wc -l; }

prepare() {
  local ds="$1" h5="$2"
  step "prepare_$ds" "$PY" scripts/prepare_real_dataset.py \
    --dataset "$ds" --hdf5 "$h5" --out-dir "data/real/$ds" \
    --sizes "$SIZES" --calibration 1000 --quality 1000 --k 10 --seed 42
}

build_indices() {
  local ds="$1" dim="$2"; shift 2
  local n idx
  for n in "$@"; do
    idx="data/real/$ds/index_n${n}_M16_seed42.bin"
    if [[ -s "$idx" ]]; then
      say "skip index n=$n (exists, $(du -h "$idx" | cut -f1))"
      continue
    fi
    step "build_${ds}_n$n" bash -c "
      start=\$(date +%s)
      build/hnsw_build --input data/real/$ds/${ds}_base_n$n.fvecs --dim $dim \
        --M 16 --ef-construction 200 --seed 42 --output '$idx'
      rc=\$?
      echo \"build_seconds=\$(( \$(date +%s) - start )) bytes=\$(stat -c%s '$idx' 2>/dev/null)\"
      exit \$rc"
  done
}

calibrate() {
  local ds="$1"; shift
  local n
  for n in "$@"; do
    step "calibrate_${ds}_n$n" "$PY" scripts/calibrate_recall_budget.py \
      --dataset "$ds" --n "$n" \
      --index "data/real/$ds/index_n${n}_M16_seed42.bin" \
      --queries "data/real/$ds/${ds}_query_calibration.fvecs" \
      --gt "data/real/$ds/${ds}_gt_n${n}_calibration_k10.u32" \
      --quality-queries "data/real/$ds/${ds}_query_quality.fvecs" \
      --quality-gt "data/real/$ds/${ds}_gt_n${n}_quality_k10.u32" \
      --targets 0.95,0.99 --transport mmap --cache-capacity 100000 \
      --out "$out/$ds/n$n"
  done
}

campaign() {
  local name="$1" ds="$2" n="$3"; shift 3
  local dir="$out/$name"
  step "$name" "$PY" scripts/run_real_rdma_campaign.py \
    --campaign "$name" --dataset "$ds" --n "$n" \
    --index "data/real/$ds/index_n${n}_M16_seed42.bin" \
    --timing-queries "data/real/$ds/${ds}_query_timing.fvecs" \
    --quality-queries "data/real/$ds/${ds}_query_quality.fvecs" \
    --quality-gt "data/real/$ds/${ds}_gt_n${n}_quality_k10.u32" \
    --calibration "$out/$ds/n$n/calibration.json" "$@"
  echo "completed_runs=$(completed_runs "$dir")" >"$status/$name.count"
  step "analyze_$name" "$PY" scripts/analyze_real_rdma.py "$dir"
}

wait_for_gist() {
  local manifest="data/real/downloads/gist-960-euclidean.hdf5.manifest.json"
  local waited=0
  while [[ ! -f "$manifest" ]]; do
    if (( waited > 21600 )); then
      say "gist download not finished after 6h; skipping gist steps"
      return 1
    fi
    sleep 60
    waited=$(( waited + 60 ))
  done
  say "gist download manifest present"
  return 0
}

say "pipeline start pid=$$"
[[ -x build/hnsw_build && -x build/storage_server && -x build/baseline1_experiment ]] || {
  say "binaries missing"; exit 1; }
df -h . | tail -1 >>"$logs/pipeline_env.log"
uptime >>"$logs/pipeline_env.log"

# ---- SIFT (primary) -----------------------------------------------------
prepare sift data/real/downloads/sift-128-euclidean.hdf5
build_indices sift 128 10000 100000 1000000
calibrate sift 10000 100000 1000000
campaign sift_n10000_smoke sift 10000 --phases quality,static --targets 0.95 \
  --configs B1,B2,B3,G1,G4,G16,G32 --reps 1 --cold 200 --warmup 300 \
  --min-timed 1000 --min-seconds 20
campaign sift_n100000_capacity sift 100000 --phases quality,capacity --targets 0.95 \
  --configs B1,B2,B3,G1,G4,G16,G32 --caches 1000,10000,100000,200000 \
  --reps "$REPS" --cache 100000
# v1 sized the shared timed count from the slowest configuration, which left the
# fastest ones at a 35.7 s window (below the 60 s floor).  v2 sizes it from the
# fastest configuration so every run clears 60 s; v1 is kept, not overwritten.
campaign sift_n100000_static_v2 sift 100000 --phases quality,static \
  --targets 0.95,0.99 --configs B1,B2,B3,G1,G4,G16,G32 --reps "$REPS" \
  --cache 100000
campaign sift_n1000000_static sift 1000000 --phases quality,static \
  --targets 0.95,0.99 --configs B1,B2,B3,G1,G4,G16,G32 --reps "$REPS" \
  --cache 100000
# Plan section 5: the same-ratio capacity supplement for the 1M index.
campaign sift_n1000000_capacity sift 1000000 --phases quality,capacity \
  --targets 0.95 --configs B3,G16,G32 --caches 1000000 --cache 100000 \
  --reps "$REPS"

# ---- GIST (cross-dataset static validation) ------------------------------
if wait_for_gist; then
  prepare gist data/real/downloads/gist-960-euclidean.hdf5
  build_indices gist 960 10000 100000 1000000
  calibrate gist 10000 100000 1000000
  campaign gist_n100000_static gist 100000 --phases quality,static \
    --targets 0.95,0.99 --configs B1,B2,B3,G1,G4,G16,G32 --reps "$REPS" \
    --cache 100000
  campaign gist_n1000000_static gist 1000000 --phases quality,static \
    --targets 0.95,0.99 --configs B1,B2,B3,G1,G4,G16,G32 --reps "$REPS" \
    --cache 100000
fi

say "pipeline finished"
for f in "$status"/*.failed; do
  [[ -f "$f" ]] && say "failed step: $(basename "$f" .failed)"
done
exit 0
