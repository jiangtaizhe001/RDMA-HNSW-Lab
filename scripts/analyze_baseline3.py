#!/usr/bin/env python3
"""Baseline 3 (compute-side neighbor-length cache) analysis and report.

Inputs
------
``--summary``      summary CSV written by ``baseline1_experiment`` (one row per
                   run).  Rows sharing a ``label`` form one experiment; the
                   Baseline 3 rows are further split by ``cache_capacity``.
``--per-query-dir`` directory holding the per-query CSVs written by
                   ``scripts/run_baseline3.sh`` (used for the hint-vs-degree
                   distribution); defaults to the summary's directory.
``--index``        optional RHNSW index, for the per-level degree histogram.
``--out-dir``      where the charts go.
``--report``       optional markdown report path.

What it computes
----------------
Per Baseline 3 run (docs/baseline3-plan.md section 7.2):

    hit_rate            = cache_hits / (cache_hits + cache_misses)
    q (underestimate)   = cache_invalidations / cache_hits
    overestimate_rate   = hint_overestimates / cache_hits
    eviction_rate       = cache_evictions / cache_updates
    avg_hint, avg_degree
    op_gain_vs_b2       = (B2.read_ops - B3.read_ops) / B2.read_ops
    model_gain_vs_b2    = (p - q) / 2          <- section 4.2 prediction
    byte_gain_vs_b1     = (B1.requested - B3.requested) / B1.requested
    total_read_ops      = read_ops + node_reads   (neighbour reads are ~2-8%)
    rtt_per_saved_op_us = (B2.mean - B3.mean) / (B2.read_ops - B3.read_ops)

Identities asserted (section 7.4, over successful runs):

    (1) requested == useful + wasted
    (2) read_ops  == hint_reads + header_reads + dependent_reads
    (3) cache_hits == hint_reads
    (4) cache_hits + cache_misses == layer_reads
    (5) cache_misses == header_reads
    (6) fallbacks == cache_invalidations
    (7) dependent_reads == fallbacks + cold_start_reads
    (8) wasted_bytes == 4 * over_read_slots_sum

Exit code is 1 when any identity is violated, so the script can gate CI.

Usage
-----
    python3 scripts/analyze_baseline3.py \
        --summary out/baseline3_summary.csv \
        --out-dir out/figures_baseline3 \
        --report out/baseline3_report.md \
        --index data/index_n10000_d128_M16.bin
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

B1 = "baseline1_fixed"
B2 = "baseline2_header_first"
B3 = "baseline3_cached_length"
NODE_BYTES = 4  # sizeof(NodeId)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def num(row: dict, key: str, default: float = 0.0) -> float:
    """Tolerant numeric accessor: Baseline 1/2 rows lack the Baseline 3
    columns entirely, so every new field must be optional."""
    value = row.get(key)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_summary(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def parse_levels(text: str | None) -> dict[int, int]:
    """Parse the compact ``0:120;1:30`` encoding used by the driver."""
    out: dict[int, int] = {}
    if not text:
        return out
    for part in str(text).split(";"):
        if not part:
            continue
        level, _, count = part.partition(":")
        try:
            out[int(level)] = int(float(count))
        except ValueError:
            continue
    return out


def node_reads_of(row: dict) -> float:
    """Node-region read count.  Older summary rows only carry node_bytes, and
    every node read is exactly sizeof(NodeDiskHeader)=24 + 4*dim bytes, so the
    count is recoverable without re-running the sweep."""
    direct = num(row, "node_reads")
    if direct:
        return direct
    dim = num(row, "dim")
    return num(row, "node_bytes") / (24 + 4 * dim) if dim else 0.0


def level_hit_rates(row: dict) -> dict[int, float]:
    hits = parse_levels(row.get("cache_hits_by_level"))
    misses = parse_levels(row.get("cache_misses_by_level"))
    out = {}
    for level in sorted(set(hits) | set(misses)):
        total = hits.get(level, 0) + misses.get(level, 0)
        if total:
            out[level] = hits.get(level, 0) / total
    return out


# --------------------------------------------------------------------------- #
# records + identities
# --------------------------------------------------------------------------- #
def build_records(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = defaultdict(lambda: {"b3": []})
    for r in rows:
        key = (r.get("dataset", ""), r.get("label", ""))
        if r.get("strategy") == B1:
            groups[key]["b1"] = r
        elif r.get("strategy") == B2:
            groups[key]["b2"] = r
        elif r.get("strategy") == B3:
            groups[key]["b3"].append(r)

    recs = []
    for (dataset, label), g in sorted(groups.items()):
        for row in sorted(g["b3"], key=lambda r: num(r, "cache_capacity")):
            recs.append(make_record(dataset, label, g.get("b1"),
                                    g.get("b2"), row))
    return recs


def make_record(dataset, label, b1, b2, b3) -> dict:
    hits = num(b3, "cache_hits")
    misses = num(b3, "cache_misses")
    lookups = hits + misses
    invalidations = num(b3, "cache_invalidations")
    updates = num(b3, "cache_updates")
    requested = num(b3, "requested_bytes")
    rec = {
        "dataset": dataset,
        "label": label,
        "strategy": b3.get("strategy"),
        "update_op": b3.get("update_op", "none"),
        "ratio": num(b3, "update_ratio"),
        "distribution": b3.get("update_distribution", "roundrobin"),
        "capacity": int(num(b3, "cache_capacity")),
        "dim": int(num(b3, "dim")),
        "M": int(num(b3, "M")),
        "queries": int(num(b3, "queries")),
        "updates": int(num(b3, "updates")),
        "hits": hits,
        "misses": misses,
        "invalidations": invalidations,
        "evictions": num(b3, "cache_evictions"),
        "fallbacks": num(b3, "fallbacks"),
        "fallback_bytes": num(b3, "fallback_bytes"),
        "cold_start_reads": num(b3, "cold_start_reads"),
        "hint_reads": num(b3, "hint_reads"),
        "header_reads": num(b3, "header_reads"),
        "dependent_reads": num(b3, "dependent_reads"),
        "hint_overestimates": num(b3, "hint_overestimates"),
        "hint_slots_sum": num(b3, "hint_slots_sum"),
        "over_read_slots_sum": num(b3, "over_read_slots_sum"),
        "requested": requested,
        "useful": num(b3, "useful_bytes"),
        "wasted": num(b3, "wasted_bytes"),
        "layer_reads": num(b3, "layer_reads"),
        "read_ops": num(b3, "read_ops"),
        "node_reads": node_reads_of(b3),
        "node_bytes": num(b3, "node_bytes"),
        "degree_sum": num(b3, "degree_sum"),
        "capacity_sum": num(b3, "capacity_sum"),
        "latency_mean": num(b3, "latency_mean_us"),
        "latency_p50": num(b3, "latency_p50_us"),
        "latency_p95": num(b3, "latency_p95_us"),
        "latency_p99": num(b3, "latency_p99_us"),
        "recall": num(b3, "recall"),
        "failed_reads": num(b3, "failed_reads"),
        "hit_rate": hits / lookups if lookups else 0.0,
        "miss_rate": misses / lookups if lookups else 0.0,
        "q": invalidations / hits if hits else 0.0,
        "overestimate_rate": num(b3, "hint_overestimates") / hits if hits else 0.0,
        "correction_rate": updates / hits if hits else 0.0,
        "eviction_rate": num(b3, "cache_evictions") / updates if updates else 0.0,
        "avg_hint": num(b3, "avg_hint"),
        # The summary only carries avg_degree; degree_sum exists per query.
        "avg_degree": (num(b3, "degree_sum") / num(b3, "layer_reads"))
        if num(b3, "degree_sum") and num(b3, "layer_reads")
        else num(b3, "avg_degree"),
        "cap_util": num(b3, "capacity_utilization"),
        "hit_rate_by_level": level_hit_rates(b3),
        "total_read_ops": num(b3, "read_ops") + node_reads_of(b3),
        "total_bytes": requested + num(b3, "node_bytes"),
        "waste_share": num(b3, "wasted_bytes") / requested if requested else 0.0,
        "fallback_byte_share": num(b3, "fallback_bytes") / requested
        if requested else 0.0,
        "fallback_per_layer": num(b3, "fallbacks") / num(b3, "layer_reads")
        if num(b3, "layer_reads") else 0.0,
    }
    # Cross-strategy deltas (only when the sweep ran the other baselines).
    if b1 is not None:
        b1_req = num(b1, "requested_bytes")
        b1_ops = num(b1, "read_ops")
        rec["b1_requested"] = b1_req
        rec["b1_read_ops"] = b1_ops
        rec["b1_latency_p50"] = num(b1, "latency_p50_us")
        rec["byte_gain_vs_b1"] = (b1_req - requested) / b1_req if b1_req else 0.0
        rec["total_gain_vs_b1"] = (
            (b1_req + num(b1, "node_bytes")) - rec["total_bytes"]
        ) / (b1_req + num(b1, "node_bytes")) if (b1_req + num(b1, "node_bytes")) else 0.0
        rec["b1_total_read_ops"] = b1_ops + node_reads_of(b1)
    if b2 is not None:
        b2_ops = num(b2, "read_ops")
        b2_req = num(b2, "requested_bytes")
        saved = b2_ops - num(b3, "read_ops")
        rec["b2_read_ops"] = b2_ops
        rec["b2_requested"] = b2_req
        rec["b2_latency_p50"] = num(b2, "latency_p50_us")
        rec["b2_latency_mean"] = num(b2, "latency_mean_us")
        rec["b2_total_read_ops"] = b2_ops + node_reads_of(b2)
        rec["op_gain_vs_b2"] = saved / b2_ops if b2_ops else 0.0
        rec["ops_saved_vs_b2"] = saved
        total_saved = rec["b2_total_read_ops"] - rec["total_read_ops"]
        rec["total_op_gain_vs_b2"] = (
            total_saved / rec["b2_total_read_ops"]
            if rec["b2_total_read_ops"] else 0.0
        )
        rec["byte_gain_vs_b2"] = (b2_req - requested) / b2_req if b2_req else 0.0
        rec["model_gain_vs_b2"] = (rec["hit_rate"] - rec["q"]) / 2.0
        mean_b2 = num(b2, "latency_mean_us")
        rec["rtt_per_saved_op_us"] = (
            (mean_b2 - rec["latency_mean"]) / saved if saved else 0.0
        )
    return rec


def check_identities(rec: dict) -> list[str]:
    bad = []
    if rec["failed_reads"] > 0:
        bad.append("failed_reads > 0 (identities assume a healthy run)")
    if abs(rec["requested"] - (rec["useful"] + rec["wasted"])) > 0.5:
        bad.append("(1) requested != useful + wasted")
    if rec["read_ops"] != (
        rec["hint_reads"] + rec["header_reads"] + rec["dependent_reads"]
    ):
        bad.append("(2) read_ops != hint_reads + header_reads + dependent_reads")
    if rec["hits"] != rec["hint_reads"]:
        bad.append("(3) cache_hits != hint_reads")
    if rec["hits"] + rec["misses"] != rec["layer_reads"]:
        bad.append("(4) cache_hits + cache_misses != layer_reads")
    if rec["misses"] != rec["header_reads"]:
        bad.append("(5) cache_misses != header_reads")
    if rec["fallbacks"] != rec["invalidations"]:
        bad.append("(6) fallbacks != cache_invalidations")
    if rec["dependent_reads"] != rec["fallbacks"] + rec["cold_start_reads"]:
        bad.append("(7) dependent_reads != fallbacks + cold_start_reads")
    if abs(rec["wasted"] - NODE_BYTES * rec["over_read_slots_sum"]) > 0.5:
        bad.append("(8) wasted_bytes != 4 * over_read_slots_sum")
    return [
        f"{rec['label']} cap={rec['capacity']}: {msg}" for msg in bad
    ]


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
def render_table(recs: list[dict]) -> str:
    head = (
        f"{'label':28s} {'cap':>7s} {'hit%':>6s} {'q%':>6s} {'over%':>6s} "
        f"{'miss%':>6s} {'evict':>7s} {'fb/layer':>8s} {'opGain2':>7s} "
        f"{'mod2':>6s} {'totG2':>6s} {'byteG1':>7s} {'p50us':>8s}"
    )
    lines = [head, "-" * len(head)]
    for r in recs:
        lines.append(
            f"{r['label']:28s} {r['capacity']:7d} {100 * r['hit_rate']:6.1f} "
            f"{100 * r['q']:6.1f} {100 * r['overestimate_rate']:6.1f} "
            f"{100 * r['miss_rate']:6.1f} {int(r['evictions']):7d} "
            f"{r['fallback_per_layer']:8.3f} "
            f"{100 * r.get('op_gain_vs_b2', 0.0):7.1f} "
            f"{100 * r.get('model_gain_vs_b2', 0.0):6.1f} "
            f"{100 * r.get('total_op_gain_vs_b2', 0.0):6.1f} "
            f"{100 * r.get('byte_gain_vs_b1', 0.0):7.2f} {r['latency_p50']:8.1f}"
        )
    return "\n".join(lines)


def model_notes(recs: list[dict]) -> str:
    paired = [r for r in recs if "op_gain_vs_b2" in r]
    if not paired:
        return ("no Baseline 2 rows paired with Baseline 3; run without "
                "SKIP_B1B2 to check the p-q model")
    lines = ["p-q model (section 4.2): op_gain_vs_b2 should track (p-q)/2"]
    worst = 0.0
    for r in paired:
        measured = r["op_gain_vs_b2"]
        model = r["model_gain_vs_b2"]
        ratio = abs(measured - model) / abs(model) if model else float("inf")
        worst = max(worst, ratio if np.isfinite(ratio) else 0.0)
        lines.append(
            f"  {r['label']} cap={r['capacity']}: measured {100 * measured:6.2f}% "
            f"vs model {100 * model:6.2f}% (p={r['hit_rate']:.3f}, "
            f"q={r['q']:.3f})"
        )
    lines.append(f"  worst relative deviation: {100 * worst:.1f}%")
    return "\n".join(lines)


def asymmetry_notes(recs: list[dict]) -> str:
    lines = ["erase vs upsert (H3: erase -> over-read waste, upsert -> fallbacks)"]
    for op in ("erase", "upsert"):
        rows = sorted([r for r in recs if r["update_op"] == op],
                      key=lambda r: (r["ratio"], r["capacity"]))
        if not rows:
            continue
        lines.append(f"  {op}:")
        for r in rows:
            lines.append(
                f"    ratio={r['ratio']:.2f} cap={r['capacity']:7d} "
                f"waste={100 * r['waste_share']:6.3f}% "
                f"q={100 * r['q']:6.2f}% "
                f"fallbacks/layer={r['fallback_per_layer']:.3f} "
                f"fb_bytes={100 * r['fallback_byte_share']:6.3f}%"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# per-query hint-vs-degree distribution
# --------------------------------------------------------------------------- #
def per_query_hint_gap(rec: dict, per_query_dir: Path) -> np.ndarray | None:
    path = per_query_dir / (
        f"baseline3_{rec['label']}_cached_length_c{rec['capacity']}.csv"
    )
    if not path.exists():
        return None
    gaps = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            hints = num(row, "hint_slots_sum")
            hint_n = num(row, "hint_reads")
            layers = num(row, "layer_reads")
            degree = num(row, "degree_sum")
            if hint_n <= 0 or layers <= 0:
                continue
            gaps.append(hints / hint_n - degree / layers)
    return np.asarray(gaps) if gaps else None


# --------------------------------------------------------------------------- #
# charts
# --------------------------------------------------------------------------- #
def make_charts(recs: list[dict], per_query_dir: Path, out_dir: Path) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - optional dependency
        print(f"[analyze] matplotlib unavailable, skipping charts: {exc}")
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    def save(fig, name):
        path = out_dir / name
        fig.tight_layout()
        fig.savefig(path, dpi=140)
        plt.close(fig)
        written.append(str(path))

    statics = [r for r in recs if r["update_op"] == "none"]

    # --- 1. hit rate vs cache capacity (global + per level) ---------------
    if statics:
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
        by_cfg = defaultdict(list)
        for r in statics:
            by_cfg[(r["label"], r["distribution"])].append(r)
        for (label, _dist), rows in sorted(by_cfg.items()):
            rows.sort(key=lambda r: r["capacity"])
            caps = [r["capacity"] for r in rows]
            axes[0].plot(caps, [100 * r["hit_rate"] for r in rows],
                         marker="o", label=label)
            levels = sorted({l for r in rows for l in r["hit_rate_by_level"]})
            for level in levels:
                pts = [(r["capacity"], 100 * r["hit_rate_by_level"][level])
                       for r in rows if level in r["hit_rate_by_level"]]
                axes[1].plot([p[0] for p in pts], [p[1] for p in pts],
                             marker="s", linestyle="--",
                             label=f"{label} L{level}")
        for ax, title in zip(
            axes,
            ("global hit rate", "hit rate per level (H1: upper levels hit more)"),
        ):
            ax.set_xscale("log")
            ax.set_xlabel("cache capacity (entries)")
            ax.set_ylabel("hit rate (%)")
            ax.set_title(title)
            ax.grid(True, alpha=0.3)
            ax.legend(fontsize=7)
        save(fig, "b3_hit_rate_vs_capacity.png")

    # --- 2. hit rate and q vs update ratio --------------------------------
    mixed = [r for r in recs if r["update_op"] in ("erase", "upsert")]
    if mixed:
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=True)
        for ax, metric, title in zip(
            axes,
            ("hit_rate", "q"),
            ("hit rate p (%)", "underestimate rate q (%)"),
        ):
            for op in ("erase", "upsert"):
                for dist in sorted({r["distribution"] for r in mixed}):
                    rows = [r for r in mixed
                            if r["update_op"] == op and r["distribution"] == dist]
                    if not rows:
                        continue
                    # One line per configuration x capacity is too noisy: keep the
                    # largest capacity per configuration (the "unbounded" case).
                    caps = defaultdict(list)
                    for r in rows:
                        caps[r["label"]].append(r)
                    series = defaultdict(list)
                    for label, rs in caps.items():
                        top = max(rs, key=lambda r: r["capacity"])
                        series[label].append((top["ratio"], 100 * top[metric]))
                    for label, pts in sorted(series.items()):
                        pts.sort()
                        ax.plot([p[0] for p in pts], [p[1] for p in pts],
                                marker="o", linestyle="-",
                                label=f"{op}/{dist} {label}")
            ax.set_xlabel("update ratio")
            ax.set_title(title)
            ax.grid(True, alpha=0.3)
        axes[0].legend(fontsize=6)
        save(fig, "b3_hit_and_q_vs_update_ratio.png")

        # --- 3. fallbacks vs update ratio ---------------------------------
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
        for op in ("erase", "upsert"):
            for dist in sorted({r["distribution"] for r in mixed}):
                rows = [r for r in mixed
                        if r["update_op"] == op and r["distribution"] == dist]
                if not rows:
                    continue
                caps = defaultdict(list)
                for r in rows:
                    caps[r["label"]].append(r)
                pts_a, pts_b = [], []
                for label, rs in caps.items():
                    top = max(rs, key=lambda r: r["capacity"])
                    pts_a.append((top["ratio"], top["fallback_per_layer"]))
                    pts_b.append((top["ratio"],
                                  100 * top["fallback_byte_share"]))
                for ax, pts in zip(axes, (pts_a, pts_b)):
                    pts.sort()
                    ax.plot([p[0] for p in pts], [p[1] for p in pts],
                            marker="o", label=f"{op}/{dist}")
        axes[0].set_ylabel("fallbacks per layer read")
        axes[1].set_ylabel("fallback bytes / requested (%)")
        for ax in axes:
            ax.set_xlabel("update ratio")
            ax.set_title("fallback cost vs updates")
            ax.grid(True, alpha=0.3)
            ax.legend(fontsize=7)
        save(fig, "b3_fallbacks_vs_update_ratio.png")

    # --- 4. hint vs true degree -------------------------------------------
    ns = [r for r in recs if r["avg_hint"] > 0 and r["avg_degree"] > 0]
    if ns:
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
        xs = np.array([r["avg_degree"] for r in ns])
        ys = np.array([r["avg_hint"] for r in ns])
        axes[0].scatter(xs, ys, s=22)
        lim = [min(xs.min(), ys.min()), max(xs.max(), ys.max())]
        axes[0].plot(lim, lim, color="gray", linestyle=":", label="hint = degree")
        axes[0].set_xlabel("mean true degree (slots)")
        axes[0].set_ylabel("mean hint (slots)")
        axes[0].set_title("hint vs true degree per run")
        axes[0].grid(True, alpha=0.3)
        axes[0].legend(fontsize=8)
        # Per-query distribution of (mean hint - mean degree) when available.
        label_for = {
            "static": "static",
            "erase": "erase",
            "upsert": "upsert",
        }
        plotted = False
        for op, name in label_for.items():
            cand = [r for r in ns if r["update_op"] == op]
            if not cand:
                continue
            top = max(cand, key=lambda r: r["capacity"])
            gaps = per_query_hint_gap(top, per_query_dir)
            if gaps is None or gaps.size == 0:
                continue
            gaps = np.sort(gaps)
            axes[1].plot(gaps, np.linspace(0, 1, gaps.size),
                         label=f"{name} (cap={top['capacity']})")
            plotted = True
        axes[1].axvline(0, color="gray", linestyle=":")
        axes[1].set_xlabel("mean hint - mean degree per query (slots)")
        axes[1].set_ylabel("CDF")
        axes[1].set_title("prediction gap distribution"
                          if plotted else "prediction gap (per-query CSV missing)")
        axes[1].grid(True, alpha=0.3)
        if plotted:
            axes[1].legend(fontsize=7)
        save(fig, "b3_hint_vs_degree.png")

    # --- 5. over-read waste, erase vs upsert ------------------------------
    if mixed:
        fig, ax = plt.subplots(figsize=(8, 4.5))
        width = 0.35
        for i, op in enumerate(("erase", "upsert")):
            ratios = sorted({r["ratio"] for r in mixed if r["update_op"] == op})
            vals = []
            for ratio in ratios:
                rs = [r for r in mixed
                      if r["update_op"] == op and r["ratio"] == ratio]
                if not rs:
                    vals.append(0.0)
                    continue
                best = max(rs, key=lambda r: r["capacity"])
                vals.append(100 * best["waste_share"])
            x = np.arange(len(ratios)) + (i - 0.5) * width
            ax.bar(x, vals, width, label=op)
        ax.set_xticks(np.arange(len(sorted({r["ratio"] for r in mixed}))))
        ax.set_xticklabels([f"{r:.1f}" for r in
                            sorted({r["ratio"] for r in mixed})])
        ax.set_xlabel("update ratio")
        ax.set_ylabel("over-read waste / requested (%)")
        ax.set_title("H3: erase over-predicts (waste), upsert under-predicts "
                     "(fallback)")
        ax.grid(True, axis="y", alpha=0.3)
        ax.legend(fontsize=8)
        save(fig, "b3_overestimate_waste.png")

    # --- 6. latency and total read ops, all three strategies --------------
    if statics:
        fig, axes = plt.subplots(2, 1, figsize=(10, 7))
        labels = [f"{r['label']}\ncap={r['capacity']}" for r in statics]
        x = np.arange(len(statics))
        width = 0.26
        for i, (key, name) in enumerate(
            (("b1_latency_p50", "B1 p50"), ("b2_latency_p50", "B2 p50"),
             ("latency_p50", "B3 p50"))
        ):
            if all(key in r for r in statics):
                axes[0].bar(x + (i - 1) * width,
                            [r[key] for r in statics], width, label=name)
        axes[0].set_xticks(x)
        axes[0].set_xticklabels(labels, rotation=25, ha="right", fontsize=7)
        axes[0].set_ylabel("p50 latency (us)")
        axes[0].set_title("mmap latency: read ops dominate nothing, so the "
                          "gain is small (H7)")
        axes[0].legend(fontsize=8)
        axes[0].grid(True, axis="y", alpha=0.3)
        for i, (key, name) in enumerate(
            (("b1_total_read_ops", "B1"), ("b2_total_read_ops", "B2"),
             ("total_read_ops", "B3"))
        ):
            if all(key in r for r in statics):
                axes[1].bar(x + (i - 1) * width,
                            [r[key] for r in statics], width, label=name)
        axes[1].set_xticks(x)
        axes[1].set_xticklabels(labels, rotation=25, ha="right", fontsize=7)
        axes[1].set_ylabel("read ops (neighbour + node)")
        axes[1].set_title("total RDMA READ operations (neighbour reads are only "
                          "2-8% of them)")
        axes[1].legend(fontsize=8)
        axes[1].grid(True, axis="y", alpha=0.3)
        save(fig, "b3_latency_and_read_ops.png")

    # --- 7. degree histograms (structural context) ------------------------
    return written


def degree_charts(index_paths, out_dir: Path) -> list[str]:
    try:
        from analyze_baseline1 import degree_histogram
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - optional dependency
        print(f"[analyze] skipping degree histograms: {exc}")
        return []
    written = []
    for path in index_paths:
        hist = degree_histogram(path)
        levels = [l for l in sorted(hist["per_level"])
                  if hist["per_level"][l]["nodes"] > 0]
        if not levels:
            continue
        fig, axes = plt.subplots(len(levels), 1,
                                 figsize=(7, 2.2 * len(levels)), squeeze=False)
        for ax, level in zip(axes[:, 0], levels):
            st = hist["per_level"][level]
            ax.hist(st["degree"], bins=range(0, st["capacity"] + 2),
                    align="left", rwidth=0.85)
            ax.axvline(st["degree"].mean(), color="red", linestyle="--",
                       label=f"mean={st['degree'].mean():.2f}")
            ax.set_xlabel("neighbor degree")
            ax.set_ylabel("nodes")
            ax.set_title(
                f"level {level}: capacity {st['capacity']}, {st['nodes']} nodes, "
                f"util {st['degree'].mean() / st['capacity']:.3f}", fontsize=9)
            ax.legend(fontsize=8)
        fig.suptitle(f"Degree histogram by level - {Path(path).stem}")
        out = out_dir / f"degree_hist_{Path(path).stem}.png"
        fig.tight_layout()
        fig.savefig(out, dpi=140)
        plt.close(fig)
        written.append(str(out))
    return written


# --------------------------------------------------------------------------- #
def parse_args(argv):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument("--per-query-dir", type=Path, default=None,
                   help="directory with the per-query CSVs (default: summary dir)")
    p.add_argument("--index", type=Path, action="append", default=[],
                   help="RHNSW index to inspect (repeatable)")
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--report", type=Path, default=None)
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    rows = read_summary(args.summary)
    recs = build_records(rows)
    if not recs:
        print("no baseline3_cached_length rows found in the summary",
              file=sys.stderr)
        return 2
    per_query_dir = args.per_query_dir or args.summary.parent

    violations = []
    for rec in recs:
        violations.extend(check_identities(rec))

    print(render_table(recs))
    print()
    print(model_notes(recs))
    print()
    print(asymmetry_notes(recs))
    if violations:
        print("\nIDENTITY VIOLATIONS:")
        for v in violations:
            print(f"  {v}")
    else:
        print("\nall Baseline 3 identities hold")

    report = ["# Baseline 3 analysis\n",
              "## per-run metrics\n", "```", render_table(recs), "```", "",
              "## p - q model\n", "```", model_notes(recs), "```", "",
              "## erase vs upsert\n", "```", asymmetry_notes(recs), "```", ""]
    report.append("## identity checks\n")
    if violations:
        report.extend(f"- VIOLATION: {v}\n" for v in violations)
    else:
        report.append("- all eight Baseline 3 identities hold "
                      "(section 7.4)\n")

    if args.out_dir is not None:
        written = make_charts(recs, per_query_dir, args.out_dir)
        written += degree_charts(args.index, args.out_dir)
        if written:
            print("\nfigures:")
            for w in written:
                print(f"  {w}")
            report.append("## figures\n")
            report.extend(f"- {w}\n" for w in written)

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text("\n".join(report))
        print(f"\nreport -> {args.report}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
