#!/usr/bin/env python3
"""Analyze the RDMA Baseline 1/2/3 sweep.

Inputs (produced by scripts/run_rdma_baselines.sh):
  out/rdma/static_summary.csv
  out/rdma/mixed_summary.csv
  out/rdma/concurrency/summary_c*.csv
  out/rdma/concurrency/groups.csv
  out/rdma/per_query/<label>_<key>.csv
  out/rdma/concurrency/per_query/<label>_<key>_c<N>.csv
  out/rdma/updates/<label>_<key>.csv

Outputs:
  out/rdma_report_tables.md
  out/figures_rdma/*.png

The script validates the byte/operation identities before reporting anything, so
a failed identity aborts with a non-zero exit code instead of silently producing
a misleading chart.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

STRATEGY_ORDER = [
    "baseline1_fixed",
    "baseline2_header_first",
    "baseline3_cached_length",
]
STRATEGY_LABEL = {
    "baseline1_fixed": "B1 fixed",
    "baseline2_header_first": "B2 header-first",
    "baseline3_cached_length": "B3 cached-length",
}
STRATEGY_KEY = {
    "baseline1_fixed": "baseline1",
    "baseline2_header_first": "baseline2",
    "baseline3_cached_length": "baseline3",
}
COLORS = {
    "baseline1_fixed": "#4C72B0",
    "baseline2_header_first": "#DD8452",
    "baseline3_cached_length": "#55A868",
}


@dataclass
class Run:
    source: str
    strategy: str
    label: str
    row: dict[str, str]
    latency: np.ndarray = field(default_factory=lambda: np.array([]))
    requested: np.ndarray = field(default_factory=lambda: np.array([]))
    read_ops: np.ndarray = field(default_factory=lambda: np.array([]))
    transport_read_ops: np.ndarray = field(default_factory=lambda: np.array([]))
    query_idx: np.ndarray = field(default_factory=lambda: np.array([]))
    client: int | None = None

    def stats(self) -> dict[str, float]:
        lat = self.latency
        if lat.size == 0:
            return {}
        return {
            "n": float(lat.size),
            "mean_us": float(lat.mean()),
            "p50_us": float(np.percentile(lat, 50)),
            "p95_us": float(np.percentile(lat, 95)),
            "p99_us": float(np.percentile(lat, 99)),
            "qps": float(1e6 * lat.size / lat.sum()),
            "requested_per_query": float(self.requested.mean()) if self.requested.size else 0.0,
            "read_ops_per_query": float(self.read_ops.mean()) if self.read_ops.size else 0.0,
            "transport_ops_per_query": float(self.transport_read_ops.mean())
            if self.transport_read_ops.size else 0.0,
        }


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def to_int(row: dict[str, str], key: str, default: int = 0) -> int:
    value = row.get(key, "")
    if value in ("", None):
        return default
    return int(float(value))


def to_float(row: dict[str, str], key: str, default: float = 0.0) -> float:
    value = row.get(key, "")
    if value in ("", None):
        return default
    return float(value)


def per_query_path(root: Path, label: str, strategy: str,
                   client: int | None = None) -> Path:
    key = STRATEGY_KEY.get(strategy, strategy)
    if client is None:
        return root / "per_query" / f"{label}_{key}.csv"
    return root / "concurrency" / "per_query" / f"{label}_{key}_c{client}.csv"


def load_run(root: Path, source: str, row: dict[str, str], warmup: int,
             client: int | None = None) -> Run:
    strategy = row["strategy"]
    label = row["label"]
    run = Run(source=source, strategy=strategy, label=label, row=row,
              client=client)
    path = per_query_path(root, label, strategy, client)
    rows = read_csv(path)
    if rows:
        keep = [r for r in rows if to_int(r, "query_idx") >= warmup]
        run.query_idx = np.array([to_int(r, "query_idx") for r in keep], dtype=int)
        run.latency = np.array([to_float(r, "latency_us") for r in keep])
        run.requested = np.array([to_int(r, "requested_bytes") for r in keep])
        run.read_ops = np.array([to_int(r, "read_ops") for r in keep])
        run.transport_read_ops = np.array(
            [to_int(r, "transport_read_ops") for r in keep])
    return run


def validate(rows: list[tuple[str, dict[str, str]]]) -> list[str]:
    problems: list[str] = []
    for source, r in rows:
        strategy = r["strategy"]
        label = r["label"]
        requested = to_int(r, "requested_bytes")
        useful = to_int(r, "useful_bytes")
        wasted = to_int(r, "wasted_bytes")
        if requested != useful + wasted:
            problems.append(
                f"{source}/{label}/{strategy}: requested {requested} != "
                f"useful {useful} + wasted {wasted}")
        if strategy == "baseline2_header_first":
            if to_int(r, "read_ops") != to_int(r, "header_reads") + to_int(r, "dependent_reads"):
                problems.append(
                    f"{source}/{label}/{strategy}: read_ops != header + dependent")
        if strategy == "baseline3_cached_length":
            read_ops = to_int(r, "read_ops")
            header = to_int(r, "header_reads")
            dependent = to_int(r, "dependent_reads")
            hits = to_int(r, "cache_hits")
            misses = to_int(r, "cache_misses")
            fallbacks = to_int(r, "fallbacks")
            invalidations = to_int(r, "cache_invalidations")
            cold = to_int(r, "cold_start_reads")
            layer_reads = to_int(r, "layer_reads")
            if to_int(r, "failed_reads") != 0:
                problems.append(f"{source}/{label}/{strategy}: failed_reads != 0")
            if read_ops != hits + header + dependent:
                problems.append(
                    f"{source}/{label}/{strategy}: read_ops != hint+header+dependent")
            if hits != to_int(r, "hint_reads"):
                problems.append(f"{source}/{label}/{strategy}: hits != hint_reads")
            if hits + misses != layer_reads:
                problems.append(f"{source}/{label}/{strategy}: hits+misses != layer_reads")
            if misses != header:
                problems.append(f"{source}/{label}/{strategy}: misses != header_reads")
            if fallbacks != invalidations:
                problems.append(f"{source}/{label}/{strategy}: fallbacks != invalidations")
            if dependent != fallbacks + cold:
                problems.append(f"{source}/{label}/{strategy}: dependent != fallbacks+cold")
            if wasted != 4 * to_int(r, "over_read_slots_sum"):
                problems.append(
                    f"{source}/{label}/{strategy}: wasted != 4*over_read_slots")
        if r.get("transport") == "rdma":
            transport = to_int(r, "transport_read_ops")
            logical = to_int(r, "read_ops") + to_int(r, "node_reads")
            if transport == 0:
                problems.append(f"{source}/{label}/{strategy}: no RDMA READ ops")
            elif logical and transport != logical:
                problems.append(
                    f"{source}/{label}/{strategy}: transport {transport} != "
                    f"read_ops+node_reads {logical}")
        if r.get("transport") == "rdma" and to_int(r, "update_failures") != 0:
            problems.append(f"{source}/{label}/{strategy}: update failures")
    return problems


def median_range(values: list[float]) -> tuple[float, float, float]:
    if not values:
        return (0.0, 0.0, 0.0)
    return (statistics.median(values), min(values), max(values))


def aggregate(runs: list[Run]) -> dict[str, dict[str, float]]:
    by_strategy: dict[str, list[dict[str, float]]] = defaultdict(list)
    for run in runs:
        stats = run.stats()
        if stats:
            by_strategy[run.strategy].append(stats)
    out: dict[str, dict[str, float]] = {}
    for strategy, stats_list in by_strategy.items():
        keys = stats_list[0].keys()
        out[strategy] = {
            key: statistics.median([s[key] for s in stats_list]) for key in keys
        }
        out[strategy]["runs"] = float(len(stats_list))
    return out


def paired_delta(runs_by_label: dict[str, dict[str, Run]],
                 base: str, other: str) -> dict[str, float]:
    means: list[float] = []
    all_diffs: list[float] = []
    for label, runs in runs_by_label.items():
        if base not in runs or other not in runs:
            continue
        a = runs[base]
        b = runs[other]
        if a.latency.size == 0 or b.latency.size == 0:
            continue
        n = min(a.latency.size, b.latency.size)
        diff = b.latency[:n] - a.latency[:n]
        means.append(float(diff.mean()))
        all_diffs.extend(diff.tolist())
    if not means:
        return {}
    arr = np.array(all_diffs)
    ci = 1.96 * float(arr.std(ddof=1)) / math.sqrt(max(1, arr.size))
    return {
        "median_run_delta_us": statistics.median(means),
        "min_run_delta_us": min(means),
        "max_run_delta_us": max(means),
        "pooled_mean_delta_us": float(arr.mean()),
        "ci95_us": ci,
    }


def fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def plot_static(static_runs: list[Run], fig_dir: Path) -> None:
    agg = aggregate(static_runs)
    strategies = [s for s in STRATEGY_ORDER if s in agg]
    if not strategies:
        return
    x = np.arange(len(strategies))
    width = 0.25
    metrics = [("p50_us", "p50"), ("p95_us", "p95"), ("p99_us", "p99")]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for i, (key, name) in enumerate(metrics):
        vals = [agg[s][key] for s in strategies]
        ax.bar(x + (i - 1) * width, vals, width, label=name)
    ax.set_xticks(x)
    ax.set_xticklabels([STRATEGY_LABEL[s] for s in strategies])
    ax.set_ylabel("latency (µs)")
    ax.set_title("Static query latency (10K, RDMA)")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "static_latency.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 3.8))
    vals = [agg[s]["qps"] for s in strategies]
    ax.bar(x, vals, color=[COLORS[s] for s in strategies])
    ax.set_xticks(x)
    ax.set_xticklabels([STRATEGY_LABEL[s] for s in strategies])
    ax.set_ylabel("queries/s")
    ax.set_title("Static query throughput (per client)")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "static_qps.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4.2))
    for s in strategies:
        lat = np.concatenate([r.latency for r in static_runs if r.strategy == s and r.latency.size])
        if lat.size:
            xs = np.sort(lat)
            ys = np.arange(1, xs.size + 1) / xs.size
            ax.plot(xs, ys, label=STRATEGY_LABEL[s], color=COLORS[s])
    ax.set_xlabel("query latency (µs)")
    ax.set_ylabel("CDF")
    ax.set_title("Static per-query latency CDF")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(fig_dir / "static_latency_cdf.png", dpi=160)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    vals_req = [agg[s]["requested_per_query"] for s in strategies]
    vals_ops = [agg[s]["read_ops_per_query"] for s in strategies]
    axes[0].bar(x, vals_req, color=[COLORS[s] for s in strategies])
    axes[0].set_xticks(x)
    axes[0].set_xticklabels([STRATEGY_LABEL[s] for s in strategies], rotation=15)
    axes[0].set_ylabel("neighbour bytes / query")
    axes[0].set_title("Neighbour-region traffic")
    axes[1].bar(x, vals_ops, color=[COLORS[s] for s in strategies])
    axes[1].set_xticks(x)
    axes[1].set_xticklabels([STRATEGY_LABEL[s] for s in strategies], rotation=15)
    axes[1].set_ylabel("neighbour read ops / query")
    axes[1].set_title("Neighbour-region reads")
    for ax in axes:
        ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(fig_dir / "static_ops_bytes.png", dpi=160)
    plt.close(fig)


def plot_mixed(mixed_runs: list[Run], fig_dir: Path) -> dict:
    groups: dict[tuple[str, str], list[Run]] = defaultdict(list)
    for run in mixed_runs:
        op = run.row.get("update_op", "")
        ratio = run.row.get("update_ratio", "")
        groups[(op, ratio)].append(run)
    if not groups:
        return {"groups": groups, "ratios": []}

    ratios = sorted({float(r) for (_, r) in groups}, key=float)
    ops = ["erase", "upsert"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, op in zip(axes, ops):
        for strategy in STRATEGY_ORDER:
            xs, ys = [], []
            for ratio in ratios:
                rs = [r for r in groups.get((op, str(ratio)), []) if r.strategy == strategy]
                stats = aggregate(rs)
                if strategy in stats:
                    xs.append(ratio)
                    ys.append(stats[strategy]["p50_us"])
            if xs:
                ax.plot(xs, ys, marker="o", label=STRATEGY_LABEL[strategy],
                        color=COLORS[strategy])
        ax.set_xlabel("update ratio")
        ax.set_title(f"{op}: query p50")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("latency (µs)")
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(fig_dir / "mixed_query_latency.png", dpi=160)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for ax, op in zip(axes, ops):
        for strategy in STRATEGY_ORDER:
            xs, ys = [], []
            for ratio in ratios:
                rs = [r for r in groups.get((op, str(ratio)), []) if r.strategy == strategy]
                if not rs:
                    continue
                vals = [to_float(r.row, "update_latency_p50_us") for r in rs]
                xs.append(ratio)
                ys.append(statistics.median(vals))
            if xs:
                ax.plot(xs, ys, marker="s", label=STRATEGY_LABEL[strategy],
                        color=COLORS[strategy])
        ax.set_xlabel("update ratio")
        ax.set_title(f"{op}: update p50")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("update latency (µs)")
    axes[0].legend()
    fig.tight_layout()
    fig.savefig(fig_dir / "mixed_update_latency.png", dpi=160)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 4))
    for op, marker in [("erase", "o"), ("upsert", "s")]:
        xs, ys = [], []
        for ratio in ratios:
            rs = [r for r in groups.get((op, str(ratio)), [])
                  if r.strategy == "baseline3_cached_length"]
            if rs:
                xs.append(ratio)
                ys.append(statistics.median([to_float(r.row, "hit_rate") for r in rs]))
        if xs:
            ax.plot(xs, ys, marker=marker, label=f"B3 {op}")
    ax.set_xlabel("update ratio")
    ax.set_ylabel("cache hit rate")
    ax.set_ylim(0, 1.05)
    ax.set_title("Baseline 3 length-cache hit rate")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(fig_dir / "mixed_b3_cache.png", dpi=160)
    plt.close(fig)

    return {"groups": groups, "ratios": ratios}


def plot_concurrency(static_runs: list[Run], conc_runs: list[Run],
                     fig_dir: Path, groups_csv: Path) -> dict:
    group_rows = read_csv(groups_csv)
    if not conc_runs and not group_rows:
        return {}
    single = aggregate(static_runs)
    # Aggregate QPS per run group = sum over the concurrent clients, then take
    # the median across repetitions.  Summing every client run across every
    # repetition would multiply the throughput by REPS.
    per_label: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    clients = 0
    for run in conc_runs:
        if run.latency.size:
            per_label[run.label][run.strategy] += 1e6 * run.latency.size / run.latency.sum()
            clients = max(clients, run.client or 0)
    group_rows_local = group_rows
    if clients == 0 and group_rows_local:
        clients = to_int(group_rows_local[0], "clients", 0)
    group_wall: dict[str, list[float]] = defaultdict(list)
    for row in group_rows:
        group_wall[row["strategy"]].append(to_float(row, "wall_ms"))

    strategies = [s for s in STRATEGY_ORDER
                  if s in single or any(s in labels for labels in per_label.values())]
    if not strategies:
        return {}
    x = np.arange(len(strategies))
    fig, ax = plt.subplots(figsize=(8, 4.2))
    single_vals = [single.get(s, {}).get("qps", 0.0) for s in strategies]
    conc_vals = []
    for s in strategies:
        vals = [labels[s] for labels in per_label.values() if s in labels]
        conc_vals.append(statistics.median(vals) if vals else 0.0)
    group_qps: dict[str, float] = {}
    queries_per_client = to_float(group_rows[0], "queries_per_client", 0) if group_rows else 0.0
    for s in strategies:
        if group_wall.get(s):
            total_queries = clients * queries_per_client
            wall_s = statistics.median(group_wall[s]) / 1000.0
            group_qps[s] = total_queries / wall_s if wall_s > 0 else 0.0
    width = 0.35
    ax.bar(x - width / 2, single_vals, width, label="1 client")
    ax.bar(x + width / 2, conc_vals, width, label=f"{clients} clients")
    ax.set_xticks(x)
    ax.set_xticklabels([STRATEGY_LABEL[s] for s in strategies])
    ax.set_ylabel("queries/s")
    ax.set_title("Static throughput: single client vs concurrent clients")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(fig_dir / "concurrency_qps.png", dpi=160)
    plt.close(fig)
    return {"single": single, "conc_qps": dict(zip(strategies, conc_vals)),
            "group_qps": group_qps, "clients": clients,
            "group_wall": dict(group_wall)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parents[1] / "out" / "rdma")
    parser.add_argument("--out-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "out" / "figures_rdma")
    parser.add_argument("--tables", type=Path,
                        default=Path(__file__).resolve().parents[1] / "out" / "rdma_report_tables.md")
    parser.add_argument("--warmup", type=int, default=200)
    args = parser.parse_args()
    root = args.root
    args.out_dir.mkdir(parents=True, exist_ok=True)

    raw_rows: list[tuple[str, dict[str, str]]] = []
    static_rows = read_csv(root / "static_summary.csv")
    mixed_rows = read_csv(root / "mixed_summary.csv")
    conc_rows: list[tuple[int, dict[str, str]]] = []
    for path in sorted((root / "concurrency").glob("summary_c*.csv")):
        try:
            client = int(path.stem.split("_c")[-1])
        except ValueError:
            client = 0
        for row in read_csv(path):
            conc_rows.append((client, row))

    for row in static_rows:
        raw_rows.append(("static", row))
    for row in mixed_rows:
        raw_rows.append(("mixed", row))
    for _, row in conc_rows:
        raw_rows.append(("concurrency", row))

    problems = validate(raw_rows)
    if problems:
        print("identity validation FAILED:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"identity validation OK: {len(raw_rows)} summary rows")

    static_runs = [load_run(root, "static", r, args.warmup) for r in static_rows]
    mixed_runs = [load_run(root, "mixed", r, args.warmup) for r in mixed_rows]
    conc_runs = [load_run(root, "concurrency", r, args.warmup, client=c)
                 for c, r in conc_rows]

    static_agg = aggregate(static_runs)
    mixed_groups: dict[tuple[str, str], list[Run]] = defaultdict(list)
    for run in mixed_runs:
        mixed_groups[(run.row.get("update_op", ""),
                      run.row.get("update_ratio", ""))].append(run)

    md: list[str] = []
    md.append("# RDMA Baseline 1/2/3 tables\n")
    md.append(f"Validated {len(raw_rows)} summary rows; warm-up queries "
              f"(query_idx < {args.warmup}) excluded from latency statistics.\n")

    md.append("## Static\n")
    rows = []
    for s in STRATEGY_ORDER:
        if s not in static_agg:
            continue
        a = static_agg[s]
        rows.append([
            STRATEGY_LABEL[s], fmt(a["qps"], 1), fmt(a["mean_us"], 1),
            fmt(a["p50_us"], 1), fmt(a["p95_us"], 1), fmt(a["p99_us"], 1),
            fmt(a["requested_per_query"], 0), fmt(a["read_ops_per_query"], 1),
            fmt(a["transport_ops_per_query"], 1), fmt(a["runs"], 0),
        ])
    md.append(table(["strategy", "QPS", "mean µs", "p50 µs", "p95 µs",
                     "p99 µs", "neigh B/query", "neigh ops/query",
                     "RDMA ops/query", "runs"], rows))
    md.append("")

    runs_by_label: dict[str, dict[str, Run]] = defaultdict(dict)
    for run in static_runs:
        runs_by_label[run.label][run.strategy] = run
    md.append("### Paired per-query deltas (static)\n")
    rows = []
    for base, other, name in [
        ("baseline1_fixed", "baseline2_header_first", "B2 - B1"),
        ("baseline1_fixed", "baseline3_cached_length", "B3 - B1"),
        ("baseline2_header_first", "baseline3_cached_length", "B3 - B2"),
    ]:
        d = paired_delta(runs_by_label, base, other)
        if d:
            rows.append([name, fmt(d["median_run_delta_us"], 1),
                         fmt(d["min_run_delta_us"], 1),
                         fmt(d["max_run_delta_us"], 1),
                         fmt(d["pooled_mean_delta_us"], 1),
                         fmt(d["ci95_us"], 1)])
    md.append(table(["pair", "median run Δ µs", "min Δ µs", "max Δ µs",
                     "pooled mean Δ µs", "95% CI µs"], rows))
    md.append("")
    if static_agg.get("baseline1_fixed"):
        base = static_agg["baseline1_fixed"]
        rows = []
        for s in STRATEGY_ORDER:
            if s not in static_agg:
                continue
            a = static_agg[s]
            rows.append([
                STRATEGY_LABEL[s],
                f"{(a['p50_us'] / base['p50_us'] - 1) * 100:+.1f}%",
                f"{(a['qps'] / base['qps'] - 1) * 100:+.1f}%",
                f"{(a['requested_per_query'] / base['requested_per_query'] - 1) * 100:+.1f}%",
                f"{(a['read_ops_per_query'] / base['read_ops_per_query'] - 1) * 100:+.1f}%",
            ])
        md.append("### Relative to B1 (static)\n")
        md.append(table(["strategy", "p50", "QPS", "neigh bytes", "neigh ops"], rows))
        md.append("")

    md.append("## Mixed workload\n")
    ratios = sorted({float(r) for (_, r) in mixed_groups}, key=float)
    rows = []
    for op in ["erase", "upsert"]:
        for ratio in ratios:
            for s in STRATEGY_ORDER:
                rs = [r for r in mixed_groups.get((op, str(ratio)), [])
                      if r.strategy == s]
                if not rs:
                    continue
                a = aggregate(rs)[s]
                row0 = rs[0].row
                rows.append([
                    op, str(ratio), STRATEGY_LABEL[s], fmt(a["qps"], 1),
                    fmt(a["p50_us"], 1), fmt(a["p95_us"], 1),
                    fmt(statistics.median([to_float(r.row, "update_latency_p50_us") for r in rs]), 0),
                    fmt(statistics.median([to_float(r.row, "hit_rate") for r in rs]) * 100, 1)
                    if s == "baseline3_cached_length" else "-",
                    fmt(statistics.median([to_float(r.row, "fallbacks") for r in rs]), 0)
                    if s == "baseline3_cached_length" else "-",
                ])
    md.append(table(["op", "ratio", "strategy", "QPS", "query p50 µs",
                     "query p95 µs", "update p50 µs", "B3 hit %", "B3 fallbacks"],
                    rows))
    md.append("")

    md.append("## Concurrency (static)\n")
    conc_dir = root / "concurrency"
    groups_csv = conc_dir / "groups.csv"
    conc_result = plot_concurrency(static_runs, conc_runs, args.out_dir, groups_csv)
    if conc_result:
        rows = []
        for s in STRATEGY_ORDER:
            if s not in conc_result["single"] and s not in conc_result["conc_qps"]:
                continue
            rows.append([
                STRATEGY_LABEL[s],
                fmt(conc_result["single"].get(s, {}).get("qps", 0.0), 1),
                fmt(conc_result["conc_qps"].get(s, 0.0), 1),
                fmt(conc_result["group_qps"].get(s, 0.0), 1),
            ])
        md.append(table(["strategy", "1 client QPS",
                         f"{conc_result['clients']} clients QPS (sum of clients)",
                         "group wall QPS (incl. setup)"], rows))
        md.append("")

    plot_static(static_runs, args.out_dir)
    plot_mixed(mixed_runs, args.out_dir)

    md.append("## Figures\n")
    for name in ["static_latency.png", "static_qps.png", "static_latency_cdf.png",
                 "static_ops_bytes.png", "mixed_query_latency.png",
                 "mixed_update_latency.png", "mixed_b3_cache.png",
                 "concurrency_qps.png"]:
        path = args.out_dir / name
        if path.exists():
            md.append(f"- `{path}`")
    md.append("")

    args.tables.parent.mkdir(parents=True, exist_ok=True)
    args.tables.write_text("\n".join(md))
    print(f"tables -> {args.tables}")
    print(f"figures -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
