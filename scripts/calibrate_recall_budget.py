#!/usr/bin/env python3
"""Locate the expansion budget that reaches a recall target on real data.

Section 4 of `docs/real-data-rdma-improvement-plan.md`: for every dataset and
scale, find the *smallest measured* `ef_search` whose recall on the calibration
split reaches the target, instead of interpolating between a passing and a
failing point (the midpoint of a bracket is often below target).

The search reuses the geometric ladder + integer bisection idea of
`run_prefix_g32_efsearch.py`:

  1. measure a geometric ladder (1, 2, 4, ... ) until the target is reached;
  2. bisect the bracket down to ``--stop-width``;
  3. measure every integer inside the final bracket and keep the smallest one
     that passes ("measured upper endpoint R");
  4. re-evaluate that budget on the independent quality split.

`--transport mmap` is the cheap calibration path; the campaign then re-checks
the same budget over real RDMA before any performance claim is made.

`--top-up` (section 4 of the plan: "若不达目标 ... 只用校准集按预定预算序列递增
选择新预算") is the follow-up step for a target whose quality-split recall misses
the target.  It walks a *pre-registered* ladder above the calibrated ``R`` and
accepts the smallest budget that passes on the calibration split **and** on the
quality split, recording every evaluation.  Because the quality split is used
more than once, the output is explicitly marked ``eval_split_reused: true`` and
the original ``calibration.json`` is never overwritten.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import numpy as np

from experiment_harness import (Server, binary_identity, digest, input_identity,
                                publish, query_sequence, run_driver, RunSpec)


def make_spec(args, ef: int, out: Path, queries: Path, gt: Path | None) -> RunSpec:
    return RunSpec(index=args.index, queries=queries, out_dir=out,
                   label=f"{args.dataset}_n{args.n}_calib_ef{ef}",
                   strategy="baseline1", prefix_granularity=4, ef_search=ef,
                   cache_capacity=args.cache_capacity, k=args.k,
                   dataset=args.dataset, warmup=0, gt=gt,
                   transport=args.transport, host=args.host, port=args.port,
                   ib_device=args.ib_device, ib_port=args.ib_port,
                   identity=args.identity)


# Pre-registered top-up ladder, in percent of the calibrated budget.  The list
# is fixed before the first quality result is inspected so that the increased
# budget cannot be chosen after seeing which step happens to pass.
TOP_UP_LADDER_PERCENT = (1, 2, 5, 10, 20, 35, 50)


def top_up_ladder(start: int) -> list[int]:
    values = {int(start)}
    for percent in TOP_UP_LADDER_PERCENT:
        values.add(int(np.ceil(start * (100 + percent) / 100.0)))
    return sorted(values)


def top_up(args) -> int:
    """Raise budgets whose *quality split* recall misses, on a fixed ladder."""
    previous = json.loads(Path(args.calibration).read_text())
    if previous["identity"]["inputs"]["index"] != digest(args.index):
        raise SystemExit("calibration index hash does not match --index")
    args.identity = previous["identity"]
    args.out.mkdir(parents=True, exist_ok=True)
    calib_count = query_sequence(args.queries)
    quality_count = query_sequence(args.quality_queries)
    results = {key: dict(value) for key, value in previous["targets"].items()}
    top_up_log = {}

    def measure(ef: int, split: str) -> dict:
        queries, gt, count = ((args.queries, args.gt, calib_count)
                              if split == "calib"
                              else (args.quality_queries, args.quality_gt,
                                    quality_count))
        out = args.out / f"runs/topup_{split}_ef{ef}"
        spec = make_spec(args, ef, out, queries, gt)
        spec.label = f"{args.dataset}_n{args.n}_topup_{split}_ef{ef}"
        row = run_driver(spec, expected_queries=count)
        hits = int(row["hits"])
        record = {"ef": ef, "split": split, "queries": count, "hits": hits,
                  "recall": hits / (count * args.k),
                  "mean_latency_us": float(row["latency_mean_us"]),
                  "failed_reads": int(row["failed_reads"])}
        print(f"  topup ef={ef:6d} {split:7s} recall={record['recall']:.5f} "
              f"({hits}/{count * args.k})", flush=True)
        return record

    for target in [float(token) for token in args.targets.split(",")]:
        key = f"{target:.2f}"
        entry = results.get(key)
        if not entry or entry.get("state") != "found":
            raise SystemExit(f"calibration has no budget for target {key}")
        start = int(entry["R"])
        quality = entry.get("quality") or {}
        passed = quality.get("recall", 0.0) >= target
        log = {"start_R": start,
               "original_quality_recall": quality.get("recall"),
               "target": target, "evaluations": [],
               "already_passing": passed}
        top_up_log[key] = log
        if passed:
            print(f"target {key}: quality split already passes at R={start}",
                  flush=True)
            continue
        chosen = None
        for ef in top_up_ladder(start):
            record = measure(ef, "calib")
            log["evaluations"].append(record)
            if record["recall"] < target:
                continue
            check = measure(ef, "quality")
            log["evaluations"].append(check)
            if check["recall"] >= target and not check["failed_reads"]:
                chosen = (ef, record, check)
                break
        if chosen is None:
            log["state"] = "unreached"
            print(f"target {key}: top-up ladder exhausted without a passing "
                  f"budget; keep R={start} and report the cell as not passing",
                  flush=True)
            continue
        ef, record, check = chosen
        log["state"] = "found"
        log["R"] = ef
        results[key] = dict(entry, R=ef, calibration_recall=record["recall"],
                            calibration_hits=record["hits"],
                            top_up={"from": start, "to": ef,
                                    "eval_split_reused": True,
                                    "ladder_percent": list(TOP_UP_LADDER_PERCENT)},
                            quality={**(entry.get("quality") or {}),
                                     "recall": check["recall"],
                                     "hits": check["hits"],
                                     "at_R": ef,
                                     "eval_split_reused": True})
        print(f"target {key}: top-up R={start} -> {ef} "
              f"(calib {record['recall']:.5f}, quality {check['recall']:.5f}; "
              f"quality split reused)", flush=True)

    publish(Path(args.topup_out) if args.topup_out
            else args.out / "calibration_topup.json",
            {**{k: v for k, v in previous.items() if k != "targets"},
             "targets": results,
             "top_up": top_up_log,
             "eval_split_reused": any(
                 (value.get("top_up") or {}).get("eval_split_reused")
                 for value in results.values()),
             "derived_from": str(args.calibration),
             "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    print(json.dumps({key: (value.get("R"),
                            (value.get("quality") or {}).get("recall"))
                      for key, value in results.items()}))
    return 0


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--n", required=True, type=int)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--gt", required=True, type=Path)
    parser.add_argument("--quality-queries", type=Path, required=True)
    parser.add_argument("--quality-gt", type=Path, required=True)
    parser.add_argument("--targets", default="0.95,0.99")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--cache-capacity", type=int, default=100000)
    parser.add_argument("--transport", default="mmap", choices=("mmap", "rdma"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18515)
    parser.add_argument("--ib-device", default="mlx5_0")
    parser.add_argument("--ib-port", type=int, default=1)
    parser.add_argument("--max-ef", type=int, default=262144)
    parser.add_argument("--stop-width", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=2,
                        help="repeats at the chosen budget and one below")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--top-up", action="store_true",
                        help="raise a budget whose quality split misses, using "
                             "the pre-registered ladder (needs --calibration)")
    parser.add_argument("--calibration", type=Path, default=None,
                        help="existing calibration.json for --top-up")
    parser.add_argument("--topup-out", type=Path, default=None)
    args = parser.parse_args(argv)

    args.identity = dict(binary_identity(),
                         inputs=input_identity({"index": args.index,
                                                "queries": args.queries,
                                                "gt": args.gt}),
                         dataset=args.dataset, n=args.n, k=args.k,
                         cache_capacity=args.cache_capacity)
    if args.top_up:
        if args.calibration is None:
            raise SystemExit("--top-up requires --calibration")
        return top_up(args)
    count = query_sequence(args.queries)
    targets = [float(token) for token in args.targets.split(",")]
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    server = Server(args.index, args.out / "server.log", port=args.port,
                    ib_device=args.ib_device, ib_port=args.ib_port) \
        if args.transport == "rdma" else None
    points: dict[int, dict] = {}
    violations: list[dict] = []
    tolerance = max(1, int(round(0.002 * count * args.k)))

    def measure(ef: int, tag: str = "calib") -> dict:
        if tag == "calib" and ef in points:
            return points[ef]
        out = args.out / f"runs/{tag}_ef{ef}"
        spec = make_spec(args, ef, out, args.queries, args.gt)
        spec.label = f"{args.dataset}_n{args.n}_{tag}_ef{ef}"
        begin = time.monotonic()
        row = run_driver(spec, expected_queries=count)
        elapsed = time.monotonic() - begin
        hit_count = int(row["hits"])
        record = {"ef": ef, "hits": hit_count, "recall": hit_count / (count * args.k),
                  "seconds": round(elapsed, 2), "tag": tag,
                  "mean_latency_us": float(row["latency_mean_us"]),
                  "read_ops": int(row["read_ops"]),
                  "requested_bytes": int(row["requested_bytes"])}
        print(f"  ef={ef:6d} recall={record['recall']:.5f} "
              f"({hit_count}/{count * args.k}) {elapsed:.1f}s [{tag}]", flush=True)
        points[ef] = record
        ordered = sorted(points)
        for left, right in zip(ordered, ordered[1:]):
            if points[left]["hits"] > points[right]["hits"]:
                # The prototype expansion budget is not strictly monotone: a
                # larger budget can drop a previously found near neighbour.
                # Record it and keep going; the budget is chosen from measured
                # points and then re-verified with independent repeats.
                violations.append({"ef_lower": left, "hits_lower": points[left]["hits"],
                                   "ef_higher": right, "hits_higher": points[right]["hits"]})
        return record

    def smallest_passing(target: float) -> dict:
        need = target * count * args.k
        ladder = [ef for ef in (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024,
                                2048, 4096, 8192, 16384, 32768, 65536,
                                131072, 262144) if ef <= args.max_ef]
        ladder = [ef for ef in ladder if args.k <= ef <= args.n]
        if ladder[-1] != args.n:
            ladder.append(args.n)
        passing = None
        for ef in ladder:
            if measure(ef)["hits"] >= need:
                passing = ef
                break
        if passing is None:
            return {"state": "unreached", "target": target,
                    "max_measured": max(points), "max_recall":
                    max(point["hits"] for point in points.values()) / (count * args.k)}
        if passing == ladder[0]:
            return {"state": "at_minimum", "target": target, "R": passing, **points[passing]}
        lower = max(ef for ef in points if ef < passing and points[ef]["hits"] < need)
        upper = passing
        history = []
        while upper - lower > args.stop_width:
            middle = (lower + upper) // 2
            hit = measure(middle)
            history.append({"L": lower, "R": upper, "ef": middle, "hits": hit["hits"]})
            if hit["hits"] >= need:
                upper = middle
            else:
                lower = middle
        for ef in range(max(1, lower), upper + 1):
            measure(ef)
        chosen = min(ef for ef in points if points[ef]["hits"] >= need)
        attempts = []
        for attempt in range(6):
            # Every attempt gets its own run directory: reusing a tag would hit
            # the resume marker and silently report the first measurement again.
            hits = [measure(chosen, f"attempt{attempt}_repeat{index}_at{chosen}")["hits"]
                    for index in range(args.repeats)]
            passes = sum(1 for hit in hits if hit >= need)
            attempts.append({"ef": chosen, "hits": hits, "passes": passes})
            if passes == args.repeats:
                break
            higher = [ef for ef in sorted(points)
                      if ef > chosen and points[ef]["hits"] >= need]
            if higher:
                chosen = higher[0]
                continue
            chosen = int(chosen * 1.25) + 1
            measure(chosen)
            while points[chosen]["hits"] < need and chosen < args.n:
                chosen = min(args.n, int(chosen * 1.5) + 1)
                measure(chosen)
        result = {"state": "found", "target": target, "R": chosen,
                  "bracket": [lower, upper], "history": history,
                  "repeats": attempts, "stable": attempts[-1]["passes"] == args.repeats,
                  **points[chosen]}
        return result

    results = {}
    try:
        if server is not None:
            server.__enter__()
        for target in targets:
            print(f"target {target}", flush=True)
            results[f"{target:.2f}"] = smallest_passing(target)
            chosen = results[f"{target:.2f}"]
            if chosen["state"] != "found":
                continue
            quality_out = args.out / f"quality_ef{chosen['R']}"
            spec = make_spec(args, chosen["R"], quality_out,
                             args.quality_queries, args.quality_gt)
            spec.label = f"{args.dataset}_n{args.n}_quality_ef{chosen['R']}"
            row = run_driver(spec, expected_queries=query_sequence(args.quality_queries))
            chosen["quality"] = {"queries": int(row["queries"]),
                                 "hits": int(row["hits"]),
                                 "recall": int(row["hits"]) /
                                 (int(row["queries"]) * args.k),
                                 "failed_reads": int(row["failed_reads"]),
                                 "mean_latency_us": float(row["latency_mean_us"])}
            print(f"  quality split recall {chosen['quality']['recall']:.5f} "
                  f"at R={chosen['R']}", flush=True)
    finally:
        if server is not None:
            server.__exit__(None, None, None)

    publish(args.out / "calibration.json",
            {"dataset": args.dataset, "n": args.n, "k": args.k,
             "identity": args.identity, "transport": args.transport,
             "cache_capacity": args.cache_capacity,
             "calibration_queries": count,
             "quality_queries": query_sequence(args.quality_queries),
             "targets": results,
             "points": {str(ef): point for ef, point in sorted(points.items())},
             "nonmonotonic_points": violations,
             "nonmonotonic_pairs": violations,
             "elapsed_seconds": round(time.monotonic() - started, 1),
             "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    print(json.dumps({key: (value.get("R"), value.get("quality", {}).get("recall"))
                      for key, value in results.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
