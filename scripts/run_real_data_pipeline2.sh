#!/usr/bin/env bash
# Second-stage pipeline: P1 quality repair, P2 mixed, P3 concurrency, P4 seeds.
#
# Continues docs/real-data-rdma-improvement-plan.md after the first pipeline
# (P0 + static P1).  Every step is idempotent: `.done` markers live in
# out/real_data_rdma/status/, campaigns resume from their own run markers.
#
# Two protocol deviations from the plan are recorded here and in the report:
#   1. the quality gate of `sift_n1000000_static` (0.95) and
#      `gist_n1000000_static` (0.99) missed its target on the independent
#      split, so a pre-registered top-up ladder re-calibrated the budget and
#      the affected cells are re-run under new campaign names
#      (`*_static_q95`, `*_static_q99`);
#   2. a full mixed matrix is not affordable (one update rewrites and
#      re-registers the whole index: 0.52 s at 100K, 6.5 s at 1M), so mixed
#      rounds run a *fixed* 100-update budget per round instead of
#      r = 0.1/0.3/0.5 of the query count.  The effective ratio is reported.
set -uo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
out="$root/out/real_data_rdma"
logs="$out/logs"
status="$out/status"
mkdir -p "$logs" "$status"
PY="${PY:-python3}"
REPS="${REPS:-5}"
UPDATES="${UPDATES:-100}"       # fixed updates per mixed round (see header)
MIX_REPS="${MIX_REPS:-$REPS}"
CONC_REPS="${CONC_REPS:-$REPS}"

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

# Budget of a target from a calibration file, so no ef_search value is hard
# coded in the driver commands (plan section 10).
budget() {
  local file="$1" target="$2" fallback="$3"
  if [[ -f "$file" ]]; then
    "$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['targets'][sys.argv[2]]['R'])" \
      "$file" "$target" 2>/dev/null || echo "$fallback"
  else
    echo "$fallback"
  fi
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
    "$@"
  echo "completed_runs=$(completed_runs "$dir")" >"$status/$name.count"
  step "analyze_$name" "$PY" scripts/analyze_real_rdma.py "$dir"
}

say "pipeline2 start pid=$$"

# ---- P1 repair: quality top-up (plan section 4) --------------------------
step calibrate_topup_sift_n1000000 "$PY" scripts/calibrate_recall_budget.py \
  --dataset sift --n 1000000 --index data/real/sift/index_n1000000_M16_seed42.bin \
  --queries data/real/sift/sift_query_calibration.fvecs \
  --gt data/real/sift/sift_gt_n1000000_calibration_k10.u32 \
  --quality-queries data/real/sift/sift_query_quality.fvecs \
  --quality-gt data/real/sift/sift_gt_n1000000_quality_k10.u32 \
  --targets 0.95,0.99 --transport mmap --cache-capacity 100000 \
  --out "$out/sift/n1000000" --top-up \
  --calibration "$out/sift/n1000000/calibration.json"
step calibrate_topup_gist_n1000000 "$PY" scripts/calibrate_recall_budget.py \
  --dataset gist --n 1000000 --index data/real/gist/index_n1000000_M16_seed42.bin \
  --queries data/real/gist/gist_query_calibration.fvecs \
  --gt data/real/gist/gist_gt_n1000000_calibration_k10.u32 \
  --quality-queries data/real/gist/gist_query_quality.fvecs \
  --quality-gt data/real/gist/gist_gt_n1000000_quality_k10.u32 \
  --targets 0.95,0.99 --transport mmap --cache-capacity 100000 \
  --out "$out/gist/n1000000" --top-up \
  --calibration "$out/gist/n1000000/calibration.json"

campaign sift_n1000000_static_q95 sift 1000000 --phases quality,static \
  --targets 0.95 --configs B1,B2,B3,G1,G4,G16,G32 --reps "$REPS" --cache 100000 \
  --calibration "$out/sift/n1000000/calibration_topup.json"

# ---- update log + checkpoint ground truth ------------------------------
step updates_sift_n100000 "$PY" scripts/prepare_update_log.py --dataset sift \
  --base data/real/sift/sift_base_n100000.fvecs --n 100000 --count "$UPDATES" \
  --slices "0,25,50,75,100" --out-dir data/real/sift/updates
step updates_sift_n1000000 "$PY" scripts/prepare_update_log.py --dataset sift \
  --base data/real/sift/sift_base_n1000000.fvecs --n 1000000 --count "$UPDATES" \
  --slices "0,25,50,75,100" --out-dir data/real/sift/updates
for n in 100000 1000000; do
  for op in erase upsert; do
    step "checkpoint_gt_sift_n${n}_${op}" "$PY" scripts/mixed_checkpoint_gt.py \
      --dataset sift --base "data/real/sift/sift_base_n${n}.fvecs" --n "$n" \
      --updates "data/real/sift/updates/sift_updates_n${n}_k${UPDATES}.fvecs" \
      --updates-manifest "data/real/sift/updates/sift_updates_n${n}_k${UPDATES}.json" \
      --op "$op" --counts 0,25,50,75,100 \
      --queries data/real/sift/sift_query_quality.fvecs \
      --out-dir "data/real/sift/checkpoint_gt_n${n}" --k 10
  done
done

# ---- P3 concurrency (no updates, cheap) --------------------------------
campaign sift_n100000_concurrency sift 100000 --phases quality,concurrency \
  --targets 0.95 --configs B1,B2,B3,G1,G4,G16,G32 --reps "$CONC_REPS" \
  --clients 1,2,4,8 --cache 100000 \
  --calibration "$out/sift/n100000/calibration.json"
campaign sift_n1000000_concurrency sift 1000000 --phases quality,concurrency \
  --targets 0.95 --configs B1,B2,B3,G1,G4,G16,G32 --reps "$CONC_REPS" \
  --clients 1,2,4,8 --cache 100000 \
  --calibration "$out/sift/n1000000/calibration_topup.json"

# ---- P2 mixed (fixed ${UPDATES}-update budget per round) ---------------
# With a fixed update budget the r knob only changes the *spacing* of the same
# 100 updates, so it is not a ratio-sensitivity experiment; two spacings are
# kept (dense / sparse) and the report says so explicitly.
campaign sift_n100000_mixed sift 100000 --phases quality,mixed \
  --targets 0.95 --configs B1,B2,B3,G1,G4,G16,G32 --reps "$MIX_REPS" \
  --mix-ratios 0.1,0.5 --mix-ops erase,upsert \
  --updates "data/real/sift/updates/sift_updates_n100000_k${UPDATES}.fvecs" \
  --updates-count "$UPDATES" --min-timed 1000 --min-seconds 1 --cache 100000 \
  --calibration "$out/sift/n100000/calibration.json"
campaign sift_n100000_checkpoints sift 100000 --phases checkpoints \
  --budgets "$(budget "$out/sift/n100000/calibration.json" 0.95 36)" \
  --targets 0.95 --configs B1,B2,B3,G1,G4,G16,G32 \
  --checkpoint-ops erase,upsert --checkpoint-ratios 0.1,0.5 \
  --checkpoint-counts 0,25,50,75,100 --checkpoint-reps 2 --cache 100000 \
  --updates "data/real/sift/updates/sift_updates_n100000_k${UPDATES}.fvecs" \
  --checkpoint-gt-dir data/real/sift/checkpoint_gt_n100000

# 1M mixed is the expensive tail: 4 configurations, 3 repeats, one spacing.
campaign sift_n1000000_mixed sift 1000000 --phases quality,mixed \
  --targets 0.95 --configs B1,B3,G16,G32 --reps 3 \
  --mix-ratios 0.5 --mix-ops erase,upsert \
  --updates "data/real/sift/updates/sift_updates_n1000000_k${UPDATES}.fvecs" \
  --updates-count "$UPDATES" --min-timed 500 --min-seconds 1 --cache 100000 \
  --calibration "$out/sift/n1000000/calibration_topup.json"

# ---- P4 graph-seed robustness -----------------------------------------
for seed in 43 44; do
  idx="data/real/sift/index_n1000000_M16_seed${seed}.bin"
  if [[ ! -s "$idx" ]]; then
    step "build_sift_n1000000_seed${seed}" bash -c "
      build/hnsw_build --input data/real/sift/sift_base_n1000000.fvecs --dim 128 \
        --M 16 --ef-construction 200 --seed ${seed} --output '$idx'"
  fi
  # A calibration file is bound to the index it was measured on, so the
  # graph seeds reuse the seed-42 budget value instead of the file.
  step "sift_n1000000_seed${seed}" "$PY" scripts/run_real_rdma_campaign.py \
    --campaign "sift_n1000000_seed${seed}" --dataset sift --n 1000000 \
    --index "$idx" --seed "$seed" \
    --timing-queries data/real/sift/sift_query_timing.fvecs \
    --quality-queries data/real/sift/sift_query_quality.fvecs \
    --quality-gt data/real/sift/sift_gt_n1000000_quality_k10.u32 \
    --phases quality,static --targets 0.95 --configs B1,B3,G16,G32 \
    --reps "$REPS" --cache 100000 \
    --budgets "$(budget "$out/sift/n1000000/calibration_topup.json" 0.95 60)"
  step "analyze_sift_n1000000_seed${seed}" "$PY" scripts/analyze_real_rdma.py \
    "$out/sift_n1000000_seed${seed}"
done

# ---- long tail: GIST 1M 0.99 with the repaired budget -----------------
campaign gist_n1000000_static_q99 gist 1000000 --phases quality,static \
  --targets 0.99 --configs B1,B2,B3,G1,G4,G16,G32 --reps "$REPS" --cache 100000 \
  --calibration "$out/gist/n1000000/calibration_topup.json"

# ---- repairs found while reviewing the first pass ---------------------
# 1. seed 44 had 5 rounds with a 56.6-59.1 s timed window (below the 60 s floor)
#    because its graph is slightly faster than seed 42; re-run with a 75 s
#    target so every round clears the floor.  The original campaign is kept.
step sift_n1000000_seed44_v2 "$PY" scripts/run_real_rdma_campaign.py \
  --campaign sift_n1000000_seed44_v2 --dataset sift --n 1000000 \
  --index data/real/sift/index_n1000000_M16_seed44.bin --seed 44 \
  --timing-queries data/real/sift/sift_query_timing.fvecs \
  --quality-queries data/real/sift/sift_query_quality.fvecs \
  --quality-gt data/real/sift/sift_gt_n1000000_quality_k10.u32 \
  --phases quality,static --targets 0.95 --configs B1,B3,G16,G32 \
  --reps "$REPS" --cache 100000 --min-seconds 75 \
  --budgets "$(budget "$out/sift/n1000000/calibration_topup.json" 0.95 60)"
step analyze_sift_n1000000_seed44_v2 "$PY" scripts/analyze_real_rdma.py \
  "$out/sift_n1000000_seed44_v2"

# 2. the 1M capacity supplement was measured at the pre-top-up budget (ef 58),
#    whose quality gate missed 0.95; repeat it at the repaired budget.
step sift_n1000000_capacity_v2 "$PY" scripts/run_real_rdma_campaign.py \
  --campaign sift_n1000000_capacity_v2 --dataset sift --n 1000000 \
  --index data/real/sift/index_n1000000_M16_seed42.bin \
  --timing-queries data/real/sift/sift_query_timing.fvecs \
  --quality-queries data/real/sift/sift_query_quality.fvecs \
  --quality-gt data/real/sift/sift_gt_n1000000_quality_k10.u32 \
  --phases quality,capacity --targets 0.95 --configs B1,B3,G16,G32 \
  --caches 1000000 --cache 100000 --reps "$REPS" \
  --calibration "$out/sift/n1000000/calibration_topup.json"
step analyze_sift_n1000000_capacity_v2 "$PY" scripts/analyze_real_rdma.py \
  "$out/sift_n1000000_capacity_v2"

# The first repeat derived 56.9-58.2 s windows (window margin 1.05); v3 keeps the
# budget and raises the margin, so every run clears the 60 s floor.
step sift_n1000000_capacity_v3 "$PY" scripts/run_real_rdma_campaign.py \
  --campaign sift_n1000000_capacity_v3 --dataset sift --n 1000000 \
  --index data/real/sift/index_n1000000_M16_seed42.bin \
  --timing-queries data/real/sift/sift_query_timing.fvecs \
  --quality-queries data/real/sift/sift_query_quality.fvecs \
  --quality-gt data/real/sift/sift_gt_n1000000_quality_k10.u32 \
  --phases quality,capacity --targets 0.95 --configs B1,B3,G16,G32 \
  --caches 1000000 --cache 100000 --reps "$REPS" --min-seconds 60 \
  --window-margin 1.20 \
  --calibration "$out/sift/n1000000/calibration_topup.json"
step analyze_sift_n1000000_capacity_v3 "$PY" scripts/analyze_real_rdma.py \
  "$out/sift_n1000000_capacity_v3"

# Extension (plan section 2): the full granularity ladder G=1/2/4/8/16/32.
campaign sift_n100000_granularity sift 100000 --phases quality,static \
  --targets 0.95,0.99 --configs B3,G1,G2,G4,G8,G16,G32 --reps "$REPS" \
  --cache 100000 --calibration "$out/sift/n100000/calibration.json"

# Machine-readable cross-campaign summary.
step summarize_real_rdma "$PY" scripts/summarize_real_rdma_campaigns.py \
  --out-dir "$out" --output "$out/summary_all.md"

say "pipeline2 finished"
for f in "$status"/*.failed; do
  [[ -f "$f" ]] && say "failed step: $(basename "$f" .failed)"
done
exit 0
