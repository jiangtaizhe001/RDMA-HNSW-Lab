#!/usr/bin/env python3
"""Analyse a real-data campaign: cold/warm-up/timed windows, run-level repeats.

Every run directory produced by `run_real_rdma_campaign.py` holds one summary
row and one per-query row per query.  This script

  * slices the per-query rows into the pre-registered windows (first ``cold``
    rows, next ``warmup`` rows, the remainder is the timed window);
  * reports latency, throughput, READ and byte accounting for the timed window
    and the cold window separately (they are never mixed);
  * aggregates over independent runs with the run - not the query - as the
    repetition unit, and reports paired ratios against B3 with a bootstrap
    confidence interval over runs;
  * summarises the quality gate (independent split with precomputed GT).

Outputs under ``<campaign>/analysis/``: ``per_run.csv``, ``aggregate.csv``,
``paired.csv``, ``quality.csv`` and ``tables.md``.
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
import sys

import numpy as np

FIELDS = ["latency_us", "requested_bytes", "useful_bytes", "wasted_bytes",
          "read_ops", "node_reads", "dependent_reads", "header_reads",
          "cache_hits", "cache_misses", "cold_start_reads", "fallbacks",
          "cold_fallbacks", "cached_fallbacks", "failed_reads",
          "cold_prefix_waste_bytes", "rounding_waste_bytes"]

SUMMARY_FIELDS = ["updates", "update_failures", "qps_end_to_end", "wall_s",
                  "update_latency_mean_us", "update_latency_p50_us",
                  "update_latency_p95_us", "update_latency_p99_us", "recall",
                  "node_count_start", "node_count_end"]


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def numeric(rows: list[dict], name: str) -> np.ndarray:
    return np.array([float(row.get(name) or 0.0) for row in rows])


def window_stats(rows: list[dict], label: str) -> dict:
    if not rows:
        return {}
    latency = numeric(rows, "latency_us")
    reads = numeric(rows, "read_ops")
    requested = numeric(rows, "requested_bytes")
    useful = numeric(rows, "useful_bytes")
    wasted = numeric(rows, "wasted_bytes")
    hits = numeric(rows, "cache_hits").sum()
    misses = numeric(rows, "cache_misses").sum()
    total = latency.sum() / 1e6
    return {
        f"{label}_queries": len(rows),
        f"{label}_mean_us": float(latency.mean()),
        f"{label}_p50_us": float(np.percentile(latency, 50)),
        f"{label}_p95_us": float(np.percentile(latency, 95)),
        f"{label}_p99_us": float(np.percentile(latency, 99)),
        f"{label}_qps": len(rows) / total if total else float("nan"),
        f"{label}_reads_per_query": float(reads.mean()),
        f"{label}_node_reads_per_query": float(numeric(rows, "node_reads").mean()),
        f"{label}_dependent_reads_per_query": float(
            numeric(rows, "dependent_reads").mean()),
        f"{label}_requested_bytes_per_query": float(requested.mean()),
        f"{label}_useful_bytes_per_query": float(useful.mean()),
        f"{label}_wasted_bytes_per_query": float(wasted.mean()),
        f"{label}_waste_ratio": float(wasted.sum() / requested.sum())
        if requested.sum() else float("nan"),
        f"{label}_cache_hit_rate": float(hits / (hits + misses))
        if hits + misses else float("nan"),
        f"{label}_cold_start_reads_per_query": float(
            numeric(rows, "cold_start_reads").mean()),
        f"{label}_cold_fallbacks_per_query": float(
            numeric(rows, "cold_fallbacks").mean()),
        f"{label}_failed_reads": float(numeric(rows, "failed_reads").sum()),
    }


def bootstrap_median(values: np.ndarray, resamples: int = 10000,
                     seed: int = 20261111) -> tuple[float, float, float]:
    if len(values) == 0:
        return (float("nan"),) * 3
    rng = np.random.default_rng(seed)
    draws = rng.choice(values, size=(resamples, len(values)), replace=True)
    medians = np.median(draws, axis=1)
    return (float(np.median(values)), float(np.percentile(medians, 2.5)),
            float(np.percentile(medians, 97.5)))


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--resamples", type=int, default=10000)
    args = parser.parse_args(argv)

    root: Path = args.campaign
    out = root / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    runs = []
    for summary in sorted(root.glob("runs/*/*/*/r*/summary.csv")):
        rel = summary.relative_to(root)
        phase = rel.parts[1]
        if phase == "checkpoint":
            # Checkpoint quality gates have no timed window; they are reported
            # by _checkpoints() instead of the timed-window tables.
            continue
        cell = rel.parts[2]
        config = rel.parts[3]
        rep = int(rel.parts[4][1:])
        cycle_file = root / f"{phase}_queries.json"
        if not cycle_file.exists():
            cycle_file = root / "static_queries.json"
        cycle = json.loads(cycle_file.read_text())
        rows = read_csv(summary.with_name("per_query.csv"))
        cold = cycle.get("cold", 1000)
        warmup = cycle.get("warmup", 2000)
        record = {"phase": phase, "cell": cell, "config": config, "rep": rep,
                  "run": str(rel.parent)}
        record.update(window_stats(rows[:cold], "cold"))
        record.update(window_stats(rows[cold:cold + warmup], "warmup"))
        record.update(window_stats(rows[cold + warmup:], "timed"))
        summary_row = read_csv(summary)[0]
        for name in SUMMARY_FIELDS:
            value = summary_row.get(name)
            record[f"run_{name}"] = float(value) if value not in (None, "") else float("nan")
        runs.append(record)

    concurrency = _concurrency(root, out, args.resamples)
    checkpoints = _checkpoints(root, out)

    if not runs:
        # A concurrency- or checkpoint-only campaign has no top-level timed
        # runs; its evidence is written by the two helpers above.
        if not concurrency["groups"] and not checkpoints:
            print(f"no completed runs under {root}/runs", file=sys.stderr)
            return 1
        (out / "tables.md").write_text(_tables([], [], [], concurrency,
                                               checkpoints))
        print(f"wrote {out}/tables.md"
              + (f", concurrency.csv ({len(concurrency['groups'])} groups)"
                 if concurrency["groups"] else "")
              + (f", checkpoint.csv ({len(checkpoints)} checks)"
                 if checkpoints else ""))
        return 0

    columns = sorted({key for run in runs for key in run})
    preferred = ["phase", "cell", "config", "rep", "run"]
    columns = preferred + [key for key in columns if key not in preferred]
    with (out / "per_run.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(runs)

    metrics = ["timed_mean_us", "timed_p50_us", "timed_p95_us", "timed_p99_us",
               "timed_qps", "timed_reads_per_query",
               "timed_node_reads_per_query", "timed_dependent_reads_per_query",
               "timed_requested_bytes_per_query", "timed_useful_bytes_per_query",
               "timed_wasted_bytes_per_query", "timed_waste_ratio",
               "timed_cache_hit_rate", "timed_cold_start_reads_per_query",
               "cold_mean_us", "cold_reads_per_query",
               "cold_cold_start_reads_per_query",
               "run_updates", "run_update_failures", "run_qps_end_to_end",
               "run_wall_s", "run_update_latency_mean_us",
               "run_update_latency_p95_us", "run_node_count_end"]
    aggregate = []
    paired = []
    grouped: dict = {}
    for run in runs:
        grouped.setdefault((run["phase"], run["cell"]), {}) \
            .setdefault(run["config"], []).append(run)
    for (phase, cell), by_config in sorted(grouped.items()):
        medians = {}
        for config, records in sorted(by_config.items()):
            entry = {"phase": phase, "cell": cell, "config": config,
                     "reps": len(records)}
            for metric in metrics:
                values = np.array([record.get(metric, float("nan"))
                                   for record in records], dtype=float)
                values = values[np.isfinite(values)]
                median, low, high = bootstrap_median(values, args.resamples)
                entry[metric] = median
                entry[f"{metric}_ci_low"] = low
                entry[f"{metric}_ci_high"] = high
                entry[f"{metric}_min"] = float(values.min()) if len(values) else float("nan")
                entry[f"{metric}_max"] = float(values.max()) if len(values) else float("nan")
            aggregate.append(entry)
            medians[config] = entry
        reference = medians.get("B3")
        if reference is None:
            continue
        for config, entry in medians.items():
            if config == "B3":
                continue
            ratios = _paired_ratios(by_config[config], by_config["B3"], metrics[:6])
            for metric, values in ratios.items():
                median, low, high = bootstrap_median(values, args.resamples)
                paired.append({"phase": phase, "cell": cell, "config": config,
                               "metric": metric, "runs": len(values),
                               "ratio_median": median, "ci_low": low,
                               "ci_high": high})

    _write_csv(out / "aggregate.csv", aggregate)
    _write_csv(out / "paired.csv", paired)

    quality = []
    for summary in sorted(root.glob("runs/quality/*/*/summary.csv")):
        rel = summary.relative_to(root)
        quality.append({"cell": rel.parts[2], "config": rel.parts[3],
                        "recall": float(read_csv(summary)[0]["recall"]),
                        "queries": int(read_csv(summary)[0]["queries"]),
                        "failed_reads": int(read_csv(summary)[0]["failed_reads"]),
                        "mean_us": float(read_csv(summary)[0]["latency_mean_us"])})
    _write_csv(out / "quality.csv", quality)

    states = _state_split(runs)
    _write_csv(out / "state_split.csv", states)
    for state in states:
        print(f"WARNING: {state['cell']} shows a two-level latency state "
              f"({state['low_center_us']:.0f} vs {state['high_center_us']:.0f} µs, "
              f"{state['gap_pct']:.1f} %) - strategy ratios are state-dominated",
              file=sys.stderr)

    (out / "tables.md").write_text(_tables(aggregate, paired, quality,
                                           concurrency, checkpoints, states))
    print(f"wrote {out}/per_run.csv ({len(runs)} runs), aggregate.csv, "
          f"paired.csv, quality.csv, tables.md"
          + (f", concurrency.csv ({len(concurrency['groups'])} groups)"
             if concurrency["groups"] else "")
          + (f", checkpoint.csv ({len(checkpoints)} checks)" if checkpoints else ""))
    return 0


def _concurrency(root: Path, out: Path, resamples: int) -> dict:
    """Aggregate the multi-client groups over one common window."""
    protocol = root / "concurrency_queries.json"
    if not protocol.exists():
        return {"groups": [], "aggregate": [], "paired": []}
    cycle = json.loads(protocol.read_text())
    cold = int(cycle.get("cold", 1000))
    warmup = int(cycle.get("warmup", 2000))
    groups = []
    grouped: dict = {}
    for summary in sorted(root.glob("runs/concurrency/*/*/r*/client*/summary.csv")):
        rel = summary.relative_to(root)
        cell, config, rep = rel.parts[2], rel.parts[3], int(rel.parts[4][1:])
        grouped.setdefault((cell, config, rep), []).append(summary)
    for (cell, config, rep), summaries in sorted(grouped.items()):
        per_client = []
        pooled_latency = []
        spans = []
        for summary in sorted(summaries):
            row = read_csv(summary)[0]
            rows = read_csv(summary.with_name("per_query.csv"))
            timed = rows[cold + warmup:]
            latency = numeric(timed, "latency_us")
            pooled_latency.append(latency)
            # The measurement window of one client is the wall time of its
            # timed queries (cold and warm-up are excluded), not the wall time
            # of the whole client process.
            window = float(latency.sum() / 1e6)
            meta_file = summary.with_name("meta.json")
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                start = datetime.strptime(meta["started_at"], "%Y-%m-%dT%H:%M:%S%z")
                spans.append((start.timestamp(),
                              start.timestamp() + float(meta["elapsed_seconds"])))
            per_client.append({
                "client": summary.parent.name,
                "timed_window_s": window,
                "timed_queries": len(timed),
                "timed_qps": float(len(timed) / window) if window else float("nan"),
                "p50_us": float(np.percentile(latency, 50)),
                "p95_us": float(np.percentile(latency, 95)),
                "p99_us": float(np.percentile(latency, 99)),
                "failed_reads": int(row["failed_reads"]),
            })
        window = min(item["timed_window_s"] for item in per_client)
        completed = sum(item["timed_queries"] * window / item["timed_window_s"]
                        for item in per_client)
        aggregate_qps = completed / window if window else float("nan")
        shares = np.array([item["timed_qps"] for item in per_client])
        jain = float(shares.sum() ** 2 / (len(shares) * (shares ** 2).sum()))
        pooled = np.concatenate(pooled_latency)
        overlap = (min(end for _, end in spans) - max(start for start, _ in spans)
                   if len(spans) >= 2 else float("nan"))
        groups.append({
            "cell": cell, "config": config, "rep": rep,
            "client_count": len(per_client), "clients": per_client,
            "window_s": window,
            "overlap_s": overlap,
            "overlap_ok": (bool(overlap >= 0.9 * window)
                           if np.isfinite(overlap) else True),
            "aggregate_qps": aggregate_qps, "jain_fairness": jain,
            "client_qps_min": float(shares.min()), "client_qps_max": float(shares.max()),
            "pooled_p50_us": float(np.percentile(pooled, 50)),
            "pooled_p95_us": float(np.percentile(pooled, 95)),
            "pooled_p99_us": float(np.percentile(pooled, 99)),
            "failed_reads": int(sum(item["failed_reads"] for item in per_client)),
        })
        for item in per_client:
            item.update({"cell": cell, "config": config, "rep": rep,
                         "group_id": f"{cell}/r{rep}"})
    _write_csv(out / "concurrency_clients.csv",
               [item for group in groups for item in group["clients"]])
    aggregate = []
    by_cell: dict = {}
    for group in groups:
        by_cell.setdefault((group["cell"], group["config"]), []).append(group)
    for (cell, config), records in sorted(by_cell.items()):
        entry = {"cell": cell, "config": config, "groups": len(records),
                 "clients": records[0]["client_count"]}
        for metric in ("aggregate_qps", "window_s", "overlap_s", "jain_fairness",
                       "pooled_p50_us", "pooled_p95_us", "pooled_p99_us",
                       "client_qps_min", "client_qps_max"):
            values = np.array([record[metric] for record in records], dtype=float)
            values = values[np.isfinite(values)]
            if not len(values):
                entry[metric] = float("nan")
                continue
            median, low, high = bootstrap_median(values, resamples)
            entry[metric] = median
            entry[f"{metric}_ci_low"] = low
            entry[f"{metric}_ci_high"] = high
        entry["overlap_ok"] = bool(all(record["overlap_ok"] for record in records))
        aggregate.append(entry)
    paired = []
    for cell in sorted({group["cell"] for group in groups}):
        reference = {group["rep"]: group for group in groups
                     if group["cell"] == cell and group["config"] == "B3"}
        if not reference:
            continue
        for config in sorted({group["config"] for group in groups
                              if group["cell"] == cell}):
            if config == "B3":
                continue
            ratios = []
            for group in groups:
                if group["cell"] != cell or group["config"] != config:
                    continue
                other = reference.get(group["rep"])
                if other and other["aggregate_qps"]:
                    ratios.append(group["aggregate_qps"] / other["aggregate_qps"])
            if not ratios:
                continue
            median, low, high = bootstrap_median(np.array(ratios), resamples)
            paired.append({"cell": cell, "config": config,
                           "metric": "aggregate_qps", "runs": len(ratios),
                           "ratio_median": median, "ci_low": low,
                           "ci_high": high})
    _write_csv(out / "concurrency.csv", aggregate)
    _write_csv(out / "concurrency_paired.csv", paired)
    return {"groups": groups, "aggregate": aggregate, "paired": paired}


def _checkpoints(root: Path, out: Path) -> list[dict]:
    rows = []
    for summary in sorted(root.glob("runs/checkpoint/*/*/r*/summary.csv")):
        rel = summary.relative_to(root)
        row = read_csv(summary)[0]
        cell = rel.parts[2]
        op, _, rest = cell.partition("_c")
        ratio = ""
        for sep in ("erase", "upsert"):
            if op.startswith(sep):
                ratio = op[len(sep):]
                op = sep
                break
        rows.append({"cell": cell, "op": op, "ratio": float(ratio or 0.0),
                     "cumulative_updates": int(rest),
                     "config": rel.parts[3], "rep": int(rel.parts[4][1:]),
                     "recall": float(row["recall"]),
                     "queries": int(row["queries"]),
                     "failed_reads": int(row["failed_reads"]),
                     "mean_us": float(row["latency_mean_us"])})
    _write_csv(out / "checkpoint.csv", rows)
    return rows


def _group(records: list[dict]) -> dict:
    grouped: dict = {}
    for record in records:
        grouped.setdefault((record["phase"], record["cell"]), []).append(record)
    return grouped


def _paired_ratios(left: list[dict], right: list[dict],
                   metrics: list[str]) -> dict[str, np.ndarray]:
    by_rep_right = {record["rep"]: record for record in right}
    ratios: dict[str, list[float]] = {metric: [] for metric in metrics}
    for record in left:
        other = by_rep_right.get(record["rep"])
        if other is None:
            continue
        for metric in metrics:
            first = record.get(metric)
            second = other.get(metric)
            if first in (None, 0) or second in (None, 0):
                continue
            if not (np.isfinite(first) and np.isfinite(second)):
                continue
            ratios[metric].append(float(first) / float(second))
    return {metric: np.array(values) for metric, values in ratios.items()}


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    columns = list(rows[0])
    for row in rows[1:]:
        for key in row:
            if key not in columns:
                columns.append(key)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _state_split(runs: list[dict], config_spread: float = 0.05,
                 minimum_share: float = 0.6) -> list[dict]:
    """Detect a two-level latency state that is independent of the strategy.

    A run-level comparison assumes one stationary machine state.  The tell-tale
    sign is that *most configurations* show an internal spread between their own
    repeats (runs of one configuration should otherwise agree within ~1 %).
    A single outlier run in one or two configurations is noise, not a state, so
    the detector requires at least ``minimum_share`` of the configurations of a
    cell to show a spread above ``config_spread``.
    """
    grouped: dict = {}
    for run in runs:
        value = run.get("timed_p50_us")
        if value is None or not np.isfinite(value):
            continue
        grouped.setdefault((run["phase"], run["cell"]), {}) \
            .setdefault(run["config"], []).append(float(value))
    findings = []
    for (phase, cell), by_config in sorted(grouped.items()):
        spreads = {}
        for config, values in by_config.items():
            ordered = sorted(values)
            if len(ordered) >= 2 and ordered[0] > 0:
                spreads[config] = (ordered[-1] - ordered[0]) / ordered[0]
        if len(by_config) < 2 or not spreads:
            continue
        spread_configs = [config for config, spread in spreads.items()
                          if spread > config_spread]
        required = max(2, int(np.ceil(minimum_share * len(by_config))))
        if len(spread_configs) < required:
            continue
        low = float(np.median([min(values) for values in by_config.values()]))
        high = float(np.median([max(values) for values in by_config.values()]))
        if low <= 0 or (high - low) / low < config_spread:
            continue
        findings.append({"phase": phase, "cell": cell,
                         "low_center_us": low, "high_center_us": high,
                         "gap_pct": (high - low) / low * 100.0,
                         "configs_with_spread": len(spread_configs),
                         "configs": len(by_config),
                         "max_config_spread_pct": round(
                             max(spreads.values()) * 100.0, 1),
                         "state_dominated": True})
    return findings


def _tables(aggregate: list[dict], paired: list[dict],
            quality: list[dict], concurrency: dict | None = None,
            checkpoints: list[dict] | None = None,
            states: list[dict] | None = None) -> str:
    lines = ["# Campaign tables", ""]
    if states:
        lines += ["## Machine-state warning (two-level latency)", "",
                  "| cell | low level µs | high level µs | gap | configs with spread | max spread |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for row in states:
            lines.append(f"| {row['cell']} | {row['low_center_us']:.0f} | "
                         f"{row['high_center_us']:.0f} | {row['gap_pct']:.1f} % | "
                         f"{row['configs_with_spread']}/{row['configs']} | "
                         f"{row['max_config_spread_pct']:.1f} % |")
        lines += ["", "Per-run medians split into two levels that cut across "
                  "every strategy, so the paired ratios of these cells are "
                  "dominated by the machine state, not by the read strategy."
                  "", ""]
    if quality:
        lines += ["## Quality gate (independent split)", "",
                  "| cell | config | recall@10 | queries | failed reads | mean µs |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for row in quality:
            lines.append(f"| {row['cell']} | {row['config']} | {row['recall']:.4f} | "
                         f"{row['queries']} | {row['failed_reads']} | "
                         f"{row['mean_us']:.1f} |")
        lines.append("")
    if aggregate:
        lines += ["## Timed window per cell (median over runs)", "",
                  "| cell | config | reps | p50 µs | p95 µs | QPS | READ/query | "
                  "req B/q | waste | hit rate |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for row in aggregate:
            lines.append(
                f"| {row['cell']} | {row['config']} | {row['reps']} | "
                f"{row['timed_p50_us']:.0f} | {row['timed_p95_us']:.0f} | "
                f"{row['timed_qps']:.1f} | {row['timed_reads_per_query']:.2f} | "
                f"{row['timed_requested_bytes_per_query']:.0f} | "
                f"{row['timed_waste_ratio']:.3f} | {row['timed_cache_hit_rate']:.3f} |")
        lines.append("")
    if paired:
        lines += ["## Paired ratios against B3 (bootstrap 95% CI over runs)", "",
                  "| cell | config | metric | ratio | CI low | CI high | runs |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for row in paired:
            lines.append(f"| {row['cell']} | {row['config']} | {row['metric']} | "
                         f"{row['ratio_median']:.4f} | {row['ci_low']:.4f} | "
                         f"{row['ci_high']:.4f} | {row['runs']} |")
        lines.append("")
    if concurrency and concurrency.get("aggregate"):
        lines += ["## Concurrency (common window, median over groups)", "",
                  "| cell | config | clients | groups | aggregate QPS | window s | "
                  "Jain | pooled p50 µs | pooled p95 µs | pooled p99 µs |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for row in concurrency["aggregate"]:
            lines.append(
                f"| {row['cell']} | {row['config']} | {row['clients']} | "
                f"{row['groups']} | {row['aggregate_qps']:.1f} | "
                f"{row['window_s']:.1f} | {row['jain_fairness']:.4f} | "
                f"{row['pooled_p50_us']:.0f} | {row['pooled_p95_us']:.0f} | "
                f"{row['pooled_p99_us']:.0f} |")
        lines.append("")
        if concurrency.get("paired"):
            lines += ["### Paired aggregate-QPS ratio against B3", "",
                      "| cell | config | ratio | CI low | CI high | groups |",
                      "| --- | --- | --- | --- | --- | --- |"]
            for row in concurrency["paired"]:
                lines.append(f"| {row['cell']} | {row['config']} | "
                             f"{row['ratio_median']:.4f} | {row['ci_low']:.4f} | "
                             f"{row['ci_high']:.4f} | {row['runs']} |")
            lines.append("")
    if checkpoints:
        grouped: dict = {}
        for row in checkpoints:
            grouped.setdefault((row["op"], row["ratio"],
                                row["cumulative_updates"]), {}) \
                .setdefault(row["config"], []).append(row["recall"])
        lines += ["## Checkpoint quality gate (exact GT on the current state)", "",
                  "| op | ratio | updates | config | reps | median recall | min | max |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for (op, ratio, count), by_config in sorted(grouped.items()):
            for config, values in sorted(by_config.items()):
                array = np.array(values, dtype=float)
                lines.append(f"| {op} | {ratio} | {count} | {config} | "
                             f"{len(array)} | {np.median(array):.4f} | "
                             f"{array.min():.4f} | {array.max():.4f} |")
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
