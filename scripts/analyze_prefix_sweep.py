#!/usr/bin/env python3
"""Analyze the local quantized-prefix G sweep.

Inputs (produced by scripts/run_prefix_sweep.sh):
  out/prefix_sweep/static_summary.csv
  out/prefix_sweep/mixed_summary.csv
  out/prefix_sweep/per_query/<label>_<key>.csv

Warm-up is not skipped by the driver, so the same trace yields two windows:
  * cold   : query_idx < --cold-split
  * steady : query_idx >= --cold-split

The script validates the byte/operation identities first and exits non-zero on
failure.  mmap runs have no transport samples, so no RDMA identity is expected.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

STRATEGY_ORDER = [
    "baseline1_fixed",
    "baseline2_header_first",
    "baseline3_cached_length",
    "quantized_prefix",
]
STRATEGY_LABEL = {
    "baseline1_fixed": "B1 fixed",
    "baseline2_header_first": "B2 header-first",
    "baseline3_cached_length": "B3 cached-length",
    "quantized_prefix": "QP",
}
STRATEGY_KEY = {
    "baseline1_fixed": "baseline1",
    "baseline2_header_first": "baseline2",
    "baseline3_cached_length": "baseline3",
    "quantized_prefix": "prefix",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def to_int(row: dict[str, str], key: str, default: int = 0) -> int:
    value = row.get(key, "")
    return default if value in ("", None) else int(float(value))


def to_float(row: dict[str, str], key: str, default: float = 0.0) -> float:
    value = row.get(key, "")
    return default if value in ("", None) else float(value)


def config_key(row: dict[str, str]) -> str:
    strategy = row["strategy"]
    base = STRATEGY_KEY.get(strategy, strategy)
    if strategy == "quantized_prefix":
        return f"{base}_g{row.get('prefix_granularity', '?')}"
    return base


def config_label(row: dict[str, str]) -> str:
    label = STRATEGY_LABEL.get(row["strategy"], row["strategy"])
    if row["strategy"] == "quantized_prefix":
        return f"{label} G{row.get('prefix_granularity', '?')}"
    return label


def config_rank(row: dict[str, str]) -> tuple[int, int]:
    strategy = row["strategy"]
    rank = STRATEGY_ORDER.index(strategy) if strategy in STRATEGY_ORDER else 99
    return (rank, to_int(row, "prefix_granularity"))


def per_query_path(root: Path, row: dict[str, str]) -> Path:
    return root / "per_query" / f"{row['label']}_{config_key(row)}.csv"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return xs[lo]
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


@dataclass
class WindowStats:
    n: int
    p50_us: float
    ops_per_query: float
    hit_rate: float
    cold_fb_per_query: float
    cached_fb_per_query: float
    wasted_bytes_per_query: float
    requested_bytes_per_query: float


def window_stats(rows: list[dict[str, str]]) -> WindowStats | None:
    if not rows:
        return None
    n = len(rows)

    def total(field: str) -> int:
        return sum(to_int(r, field) for r in rows)

    hits, misses = total("cache_hits"), total("cache_misses")
    return WindowStats(
        n=n,
        p50_us=percentile([to_float(r, "latency_us") for r in rows], 50),
        ops_per_query=total("read_ops") / n,
        hit_rate=hits / (hits + misses) if hits + misses else float("nan"),
        cold_fb_per_query=total("cold_fallbacks") / n,
        cached_fb_per_query=total("cached_fallbacks") / n,
        wasted_bytes_per_query=total("wasted_bytes") / n,
        requested_bytes_per_query=total("requested_bytes") / n,
    )


def med(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def fmt(value: float, digits: int = 2) -> str:
    if math.isnan(value):
        return "NA"
    return f"{value:.{digits}f}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def validate(rows: list[dict[str, str]]) -> list[str]:
    problems: list[str] = []
    for r in rows:
        strategy, label = r["strategy"], r["label"]
        where = f"{label}/{strategy}"
        if to_int(r, "schema_version", -1) != 2:
            problems.append(f"{where}: schema_version != 2")
        if to_int(r, "failed_reads") != 0:
            problems.append(f"{where}: failed_reads != 0")
        if to_int(r, "requested_bytes") != (
                to_int(r, "useful_bytes") + to_int(r, "wasted_bytes")):
            problems.append(f"{where}: requested != useful + wasted")
        if strategy == "baseline2_header_first" and to_int(r, "read_ops") != (
                to_int(r, "header_reads") + to_int(r, "dependent_reads")):
            problems.append(f"{where}: read_ops != header + dependent")
        if strategy == "baseline3_cached_length":
            if to_int(r, "read_ops") != (to_int(r, "hint_reads") +
                                         to_int(r, "header_reads") +
                                         to_int(r, "dependent_reads")):
                problems.append(f"{where}: B3 read_ops identity")
            if to_int(r, "cache_hits") != to_int(r, "hint_reads"):
                problems.append(f"{where}: cache_hits != hint_reads")
            if to_int(r, "cache_hits") + to_int(r, "cache_misses") != to_int(r, "layer_reads"):
                problems.append(f"{where}: hits+misses != layer_reads")
            if to_int(r, "fallbacks") != to_int(r, "cache_invalidations"):
                problems.append(f"{where}: fallbacks != invalidations")
            if to_int(r, "dependent_reads") != (
                    to_int(r, "fallbacks") + to_int(r, "cold_start_reads")):
                problems.append(f"{where}: dependent != fallbacks + cold_start")
            if to_int(r, "wasted_bytes") != 4 * to_int(r, "over_read_slots_sum"):
                problems.append(f"{where}: wasted != 4*over_read_slots")
        if strategy == "quantized_prefix":
            if to_int(r, "prefix_reads") != to_int(r, "layer_reads"):
                problems.append(f"{where}: prefix_reads != layer_reads")
            if to_int(r, "cache_hits") + to_int(r, "cache_misses") != to_int(r, "layer_reads"):
                problems.append(f"{where}: hits+misses != layer_reads")
            if to_int(r, "read_ops") != (
                    to_int(r, "prefix_reads") + to_int(r, "fallbacks")):
                problems.append(f"{where}: read_ops != prefix + fallbacks")
            if to_int(r, "dependent_reads") != to_int(r, "fallbacks"):
                problems.append(f"{where}: dependent != fallbacks")
            if to_int(r, "fallbacks") != (
                    to_int(r, "cached_fallbacks") + to_int(r, "cold_fallbacks")):
                problems.append(f"{where}: fallbacks != cached + cold")
            if to_int(r, "fallback_bytes") != (
                    to_int(r, "cached_fallback_bytes") +
                    to_int(r, "cold_fallback_bytes")):
                problems.append(f"{where}: fallback byte split")
            if to_int(r, "wasted_bytes") != (
                    to_int(r, "rounding_waste_bytes") +
                    to_int(r, "drift_waste_bytes") +
                    to_int(r, "cold_prefix_waste_bytes")):
                problems.append(f"{where}: wasted split")
    return problems


def collect_windows(root: Path, rows: list[dict[str, str]], split: int,
                    group_fields: tuple[str, ...]
                    ) -> dict[tuple, tuple[list[WindowStats], list[WindowStats]]]:
    grouped: dict[tuple, tuple[list[WindowStats], list[WindowStats]]] = defaultdict(
        lambda: ([], []))
    for row in rows:
        samples = read_csv(per_query_path(root, row))
        if not samples:
            continue
        key = tuple(row.get(field, "") for field in group_fields)
        cold, steady = grouped[key]
        cold.append(window_stats(samples[:split]))
        steady.append(window_stats(samples[split:]))
    return grouped


def render_window_table(grouped, first_fields: list[str], title: str) -> str:
    lines = [f"### {title}\n"]
    headers = first_fields + ["p50 µs", "ops/query", "hit %",
                              "cold fb/query", "cached fb/query",
                              "wasted B/query", "requested B/query"]
    body = []
    for key in sorted(grouped, key=lambda k: config_rank(
            {"strategy": k[0], "prefix_granularity": k[1]})):
        cold, steady = grouped[key]
        for name, stats in (("cold", cold), ("steady", steady)):
            rows = [s for s in stats if s is not None]
            if not rows:
                continue
            values = [name] + [str(x) for x in key]
            if key[0] != "quantized_prefix":
                values[2] = "-"  # G does not select a Baseline
            values += [
                fmt(med([s.p50_us for s in rows]), 1),
                fmt(med([s.ops_per_query for s in rows]), 2),
                fmt(med([s.hit_rate for s in rows]) * 100, 1),
                fmt(med([s.cold_fb_per_query for s in rows]), 2),
                fmt(med([s.cached_fb_per_query for s in rows]), 2),
                fmt(med([s.wasted_bytes_per_query for s in rows]), 1),
                fmt(med([s.requested_bytes_per_query for s in rows]), 1),
            ]
            body.append(values)
    lines.append(table(["window"] + headers, body))
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path,
        default=Path(__file__).resolve().parents[1] / "out" / "prefix_sweep")
    parser.add_argument(
        "--tables", type=Path,
        default=Path(__file__).resolve().parents[1] / "out" / "prefix_sweep_tables.md")
    parser.add_argument("--cold-split", type=int, default=200)
    parser.add_argument("--mixed-cold-split", type=int, default=50)
    args = parser.parse_args()

    static_rows = read_csv(args.root / "static_summary.csv")
    mixed_rows = read_csv(args.root / "mixed_summary.csv")
    all_rows = static_rows + mixed_rows
    problems = validate(all_rows)
    if problems:
        print("identity validation FAILED:")
        for problem in problems:
            print("  " + problem)
        return 1
    print(f"identity validation OK: {len(all_rows)} summary rows")

    md = ["# Quantized-prefix local mmap sweep\n"]
    md.append(f"Validated {len(all_rows)} summary rows (schema_version 2). "
              f"Cold window = first {args.cold_split} static queries / "
              f"{args.mixed_cold_split} mixed queries; these are mmap runs, so "
              "latency is not an RDMA measurement.\n")

    static_groups = collect_windows(
        args.root, static_rows, args.cold_split,
        ("strategy", "prefix_granularity", "cache_capacity"))
    # Split by cache so each table stays readable.
    for cache in sorted({key[2] for key in static_groups}, key=int):
        subset = {key: value for key, value in static_groups.items()
                  if key[2] == cache}
        lines = render_window_table(
            subset, ["strategy", "G", "cache"],
            f"Static, cache capacity {cache}")
        md.append(lines)

    md.append("## Prefix granularity (steady window)\n")
    rows = []
    base_by_cache: dict[str, list[WindowStats]] = {}
    for key, (_, steady) in static_groups.items():
        strategy, g, cache = key
        if strategy == "baseline3_cached_length":
            base_by_cache[cache] = steady
    for key in sorted(static_groups, key=lambda k: (
            int(k[2]), config_rank({"strategy": k[0],
                                    "prefix_granularity": k[1]}))):
        strategy, g, cache = key
        steady = [s for s in static_groups[key][1] if s is not None]
        if strategy != "quantized_prefix" or not steady:
            continue
        b3 = [s for s in base_by_cache.get(cache, []) if s is not None]
        rows.append([
            cache, g,
            fmt(med([s.p50_us for s in steady]), 1),
            fmt(med([s.ops_per_query for s in steady]), 2),
            fmt(med([s.hit_rate for s in steady]) * 100, 1),
            fmt(med([s.cold_fb_per_query for s in steady]), 2),
            fmt(med([s.cached_fb_per_query for s in steady]), 2),
            fmt(med([s.wasted_bytes_per_query for s in steady]), 1),
            fmt(med([s.ops_per_query for s in steady]) -
                med([s.ops_per_query for s in b3]), 2) if b3 else "NA",
        ])
    md.append(table(["cache", "G", "p50 µs", "ops/query", "hit %",
                     "cold fb/query", "cached fb/query", "wasted B/query",
                     "ops vs B3"], rows))
    md.append("")

    mixed_groups = collect_windows(
        args.root, mixed_rows, args.mixed_cold_split,
        ("strategy", "prefix_granularity", "update_op", "update_ratio"))
    md.append("## Mixed upsert\n")
    rows = []
    for key in sorted(mixed_groups, key=lambda k: (
            float(k[3]), config_rank({"strategy": k[0],
                                      "prefix_granularity": k[1]}))):
        strategy, g, op, ratio = key
        steady = [s for s in mixed_groups[key][1] if s is not None]
        if not steady:
            continue
        rows.append([
            op, ratio, STRATEGY_LABEL.get(strategy, strategy), g,
            fmt(med([s.p50_us for s in steady]), 1),
            fmt(med([s.ops_per_query for s in steady]), 2),
            fmt(med([s.hit_rate for s in steady]) * 100, 1),
            fmt(med([s.cold_fb_per_query for s in steady]), 2),
            fmt(med([s.cached_fb_per_query for s in steady]), 2),
            fmt(med([s.wasted_bytes_per_query for s in steady]), 1),
        ])
    md.append(table(["op", "ratio", "strategy", "G", "p50 µs", "ops/query",
                     "hit %", "cold fb/query", "cached fb/query",
                     "wasted B/query"], rows))
    md.append("")

    md.append("## Update rate\n")
    rows = []
    seen: set[tuple[str, str, str]] = set()
    for r in mixed_rows:
        ratio = r.get("update_ratio", "")
        op = r.get("update_op", "")
        if (ratio, op) in seen:
            continue
        seen.add((ratio, op))
        queries = to_int(r, "queries")
        updates = to_int(r, "updates")
        total = queries + updates
        rows.append([
            op, ratio, str(updates), str(queries),
            fmt(updates / queries, 3) if queries else "NA",
            fmt(updates / total, 3) if total else "NA",
        ])
    md.append(table(["op", "ratio", "updates", "queries", "updates/query",
                     "updates/(updates+queries)"], rows))
    md.append("")

    md.append("## Cost-model check (summary totals)\n")
    rows = []
    for strategy in ("baseline3_cached_length", "quantized_prefix"):
        rs = [r for r in static_rows if r["strategy"] == strategy]
        if not rs:
            continue
        predicted, measured = [], []
        for r in rs:
            queries = max(1, to_int(r, "queries"))
            extra = to_int(r, "fallbacks")
            if strategy == "baseline3_cached_length":
                extra += to_int(r, "cold_start_reads")
            predicted.append((to_int(r, "layer_reads") + extra) / queries)
            measured.append(to_int(r, "read_ops") / queries)
        rows.append([
            STRATEGY_LABEL[strategy],
            fmt(med(predicted), 3), fmt(med(measured), 3),
            fmt(max(abs(a - b) for a, b in zip(predicted, measured)), 3),
        ])
    md.append(table(["strategy", "predicted ops/query", "measured ops/query",
                     "max abs error"], rows))
    md.append("")

    args.tables.parent.mkdir(parents=True, exist_ok=True)
    args.tables.write_text("\n".join(md))
    print(f"tables -> {args.tables}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
