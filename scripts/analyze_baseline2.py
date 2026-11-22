#!/usr/bin/env python3
"""Baseline 2 (header-first) analysis, charts and report (docs/baseline2-plan.md T7/T9).

Inputs
------
``--summary`` : the summary CSV written by ``baseline1_experiment`` (one row per
    run).  Rows for ``strategy == baseline1_fixed`` and
    ``strategy == baseline2_header_first`` that share a ``label`` form a pair.
``--index``   : optional RHNSW ``.bin`` index, parsed to build the per-level
    degree histogram (reuses ``analyze_baseline1``).

What it computes per pair
-------------------------
Neighbour-region byte ledger and the Baseline 2 specific identities:

    saved_bytes        = B1.requested - B2.requested        (== B1.wasted)
    saving_pct_layer   = saved_bytes / B1.requested
    saving_pct_total   = saved_bytes / (B1.node_bytes + B1.requested)
    extra_ops          = B2.read_ops - B1.read_ops          (== B2.dependent_reads)
    ops_penalty_pct    = extra_ops / B1.read_ops
    latency_delta_p50  = B2.p50 - B1.p50
    rtt_per_dep_read   = (B2.mean - B1.mean) / B2.dependent_reads   [us]

Identities asserted (docs/baseline2-plan.md section 8.3):

    (1) requested == useful + wasted
    (2) read_ops  == header_reads + dependent_reads        (Baseline 2)
    (3) dependent_reads + zero_degree_layers == layer_reads (Baseline 2)
    (4) B1.requested - B2.requested == B1.wasted

Usage
-----
    python3 scripts/analyze_baseline2.py \
        --summary out/baseline2_summary.csv \
        --index data/index_n10000_d128_M16.bin \
        --out-dir out/figures_baseline2 \
        --report out/baseline2_report.md
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_baseline1 import degree_histogram, format_histogram  # noqa: E402

B1 = "baseline1_fixed"
B2 = "baseline2_header_first"

INT_COLS = [
    "dim", "node_count_start", "node_count_end", "M", "queries", "updates",
    "requested_bytes", "useful_bytes", "wasted_bytes", "node_bytes", "read_ops",
    "layer_reads", "header_reads", "dependent_reads", "zero_degree_layers",
    "failed_reads",
]
FLOAT_COLS = [
    "ef_construction", "ef_search", "k", "update_ratio", "qps",
    "qps_end_to_end", "wall_s", "latency_mean_us", "latency_p50_us",
    "latency_p95_us", "latency_p99_us", "waste_ratio", "neighbor_share",
    "capacity_utilization", "avg_degree", "recall",
]


# --------------------------------------------------------------------------- #
# summary parsing / pairing
# --------------------------------------------------------------------------- #
def read_summary(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for key in INT_COLS:
            r[key] = int(float(r[key]))
        for key in FLOAT_COLS:
            if key in r:
                r[key] = float(r[key])
    return rows


def pair_rows(rows: list[dict]) -> list[dict]:
    """Group by (dataset, label) and pair Baseline 1 with Baseline 2."""
    by_key: dict[tuple, dict] = defaultdict(dict)
    for r in rows:
        by_key[(r["dataset"], r["label"])][r["strategy"]] = r
    pairs = []
    for (dataset, label), members in sorted(by_key.items()):
        if B1 in members and B2 in members:
            pairs.append({"dataset": dataset, "label": label,
                          "b1": members[B1], "b2": members[B2]})
    return pairs


def compute(pairs: list[dict]) -> tuple[list[dict], list[str]]:
    recs, violations = [], []
    for p in pairs:
        b1, b2 = p["b1"], p["b2"]
        saved = b1["requested_bytes"] - b2["requested_bytes"]
        total = b1["node_bytes"] + b1["requested_bytes"]
        extra_ops = b2["read_ops"] - b1["read_ops"]
        dep = b2["dependent_reads"]
        rec = {
            "dataset": p["dataset"], "label": p["label"],
            "dim": b1["dim"], "M": b1["M"], "update_op": b1["update_op"],
            "ratio": b1["update_ratio"], "queries": b1["queries"],
            "b1_requested": b1["requested_bytes"], "b1_useful": b1["useful_bytes"],
            "b1_wasted": b1["wasted_bytes"], "b2_requested": b2["requested_bytes"],
            "b2_useful": b2["useful_bytes"], "b2_wasted": b2["wasted_bytes"],
            "node_bytes": b1["node_bytes"],
            "saved_bytes": saved,
            "saving_pct_layer": saved / b1["requested_bytes"] if b1["requested_bytes"] else 0.0,
            "saving_pct_total": saved / total if total else 0.0,
            "b1_read_ops": b1["read_ops"], "b2_read_ops": b2["read_ops"],
            "header_reads": b2["header_reads"], "dependent_reads": dep,
            "zero_degree_layers": b2["zero_degree_layers"],
            "extra_ops": extra_ops,
            "ops_penalty_pct": extra_ops / b1["read_ops"] if b1["read_ops"] else 0.0,
            "b1_mean_us": b1["latency_mean_us"], "b2_mean_us": b2["latency_mean_us"],
            "b1_p50_us": b1["latency_p50_us"], "b2_p50_us": b2["latency_p50_us"],
            "b1_p95_us": b1["latency_p95_us"], "b2_p95_us": b2["latency_p95_us"],
            "latency_delta_p50": b2["latency_p50_us"] - b1["latency_p50_us"],
            "latency_delta_pct_p50": (b2["latency_p50_us"] - b1["latency_p50_us"])
            / b1["latency_p50_us"] if b1["latency_p50_us"] else 0.0,
            "rtt_per_dep_read_us": (b2["latency_mean_us"] - b1["latency_mean_us"]) / dep
            if dep else 0.0,
            "b1_avg_degree": b1["avg_degree"], "b2_avg_degree": b2["avg_degree"],
            "b1_cap_util": b1["capacity_utilization"],
            "b1_layer_reads": b1["layer_reads"], "b2_layer_reads": b2["layer_reads"],
            "recall_b1": b1["recall"], "recall_b2": b2["recall"],
        }
        recs.append(rec)

        # --- identity checks (docs/baseline2-plan.md section 8.3) ----------
        def bad(msg):
            violations.append(f"{rec['label']}: {msg}")

        if b2["wasted_bytes"] != 0:
            bad(f"B2 wasted_bytes={b2['wasted_bytes']} != 0")
        if b1["requested_bytes"] - b2["requested_bytes"] != b1["wasted_bytes"]:
            bad("identity 4 broken: B1.requested-B2.requested != B1.wasted")
        if b2["read_ops"] != b2["header_reads"] + b2["dependent_reads"]:
            bad("identity 2 broken: read_ops != header_reads+dependent_reads")
        if b2["dependent_reads"] + b2["zero_degree_layers"] != b2["layer_reads"]:
            bad("identity 3 broken: dependent+zero_degree != layer_reads")
        if b1["useful_bytes"] != b2["useful_bytes"]:
            bad("B1 useful != B2 useful")
        if b1["recall"] != b2["recall"]:
            bad(f"recall differs: B1={b1['recall']} B2={b2['recall']}")
    return recs, violations


# --------------------------------------------------------------------------- #
# text tables
# --------------------------------------------------------------------------- #
def render_table(recs: list[dict]) -> str:
    cols = ["label", "b1_requested", "b1_wasted", "b2_requested",
            "saved_bytes", "saving_pct_layer", "saving_pct_total",
            "b1_read_ops", "b2_read_ops", "extra_ops", "ops_penalty_pct",
            "b1_p50_us", "b2_p50_us", "latency_delta_pct_p50"]
    head = " | ".join(cols)
    lines = [head, "-" * len(head)]
    for r in recs:
        cells = []
        for c in cols:
            v = r[c]
            if c in ("saving_pct_layer", "saving_pct_total", "ops_penalty_pct",
                     "latency_delta_pct_p50"):
                cells.append(f"{100 * v:.3f}%")
            elif isinstance(v, float):
                cells.append(f"{v:.3f}")
            else:
                cells.append(str(v))
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def model_notes(recs: list[dict]) -> str:
    """Static model checks: saving share and the round-trip break-even point."""
    R = 1.5e-6            # assumed one dependent RDMA READ round trip (s)
    W = 12.5e9            # assumed effective bandwidth 100 GbE (B/s)
    S_star = R * W        # bytes that justify one extra round trip
    lines = [
        "model checks (docs/baseline2-plan.md section 5):",
        f"  assumed round trip R={R * 1e6:.2f} us, bandwidth W={W / 1e9:.2f} GB/s"
        f"  ->  break-even S* = R*W = {S_star:.0f} B",
        "  maximum bytes Baseline 2 can save per level-0 layer = 4*cap = 8*M:",
    ]
    for M in sorted({r["M"] for r in recs}):
        lines.append(f"    M={M:>3}: 8*M = {8 * M:>5} B  "
                     f"(S* / 8M = {S_star / (8 * M):.0f}x)")
    statics = [r for r in recs if r["update_op"] == "none"]
    if statics:
        lines.append("  measured (static):")
        for r in statics:
            lines.append(
                f"    {r['label']}: saving {100 * r['saving_pct_layer']:.2f}% of "
                f"neighbour region, {100 * r['saving_pct_total']:.3f}% of total "
                f"bytes; extra ops +{100 * r['ops_penalty_pct']:.1f}%; "
                f"d_latency(p50) {100 * r['latency_delta_pct_p50']:+.1f}%")
    return "\n".join(lines)


def dynamic_notes(recs: list[dict]) -> str:
    lines = ["dynamic workload: saved bytes are flat, so the saving *share* falls"
             " only because erase/upsert add node-region traffic (plan section 5):"]
    for op in ("erase", "upsert"):
        rows = sorted([r for r in recs if r["update_op"] == op],
                      key=lambda r: (r["label"].split("_")[0], r["ratio"]))
        if not rows:
            continue
        lines.append(f"  {op}:")
        for r in rows:
            lines.append(
                f"    {r['label']}: saved={r['saved_bytes']} B, "
                f"layer {100 * r['saving_pct_layer']:.2f}%, "
                f"total {100 * r['saving_pct_total']:.3f}%, "
                f"ops +{100 * r['ops_penalty_pct']:.1f}%")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# charts
# --------------------------------------------------------------------------- #
def make_charts(recs: list[dict], hist_entries, out_dir: Path) -> list[str]:
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
    labels = [r["label"] for r in statics]
    x = np.arange(len(statics))

    # --- figure 1: neighbour-region byte ledger, B1 vs B2 (H1) -------------
    if statics:
        fig, ax = plt.subplots(figsize=(9, 4.5))
        width = 0.38
        b1u = np.array([r["b1_useful"] for r in statics]) / 1e6
        b1w = np.array([r["b1_wasted"] for r in statics]) / 1e6
        b2u = np.array([r["b2_useful"] for r in statics]) / 1e6
        b2w = np.array([r["b2_wasted"] for r in statics]) / 1e6
        ax.bar(x - width / 2, b1u, width, label="B1 useful")
        ax.bar(x - width / 2, b1w, width, bottom=b1u, label="B1 wasted")
        ax.bar(x + width / 2, b2u, width, label="B2 useful")
        ax.bar(x + width / 2, b2w, width, bottom=b2u, label="B2 wasted (=0)")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        ax.set_ylabel("neighbour-region bytes (MB)")
        ax.set_title("Baseline 2 eliminates neighbour-region waste (static)")
        ax.legend(fontsize=8)
        ax.grid(True, axis="y", alpha=0.3)
        save(fig, "b2_bytes_ledger.png")

        # --- figure 2: read ops and dependent reads -----------------------
        fig, ax = plt.subplots(figsize=(9, 4.5))
        b1ops = np.array([r["b1_read_ops"] for r in statics]) / 1e3
        b2ops = np.array([r["b2_read_ops"] for r in statics]) / 1e3
        dep = np.array([r["dependent_reads"] for r in statics]) / 1e3
        ax.bar(x - width / 2, b1ops, width, label="B1 read_ops")
        ax.bar(x + width / 2, b2ops, width, label="B2 read_ops")
        ax.bar(x + width / 2, dep, width, label="B2 dependent_reads",
               hatch="//", edgecolor="black", fill=False)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        ax.set_ylabel("neighbour-region reads (k)")
        ax.set_title("Baseline 2 doubles the read-op count (one extra read / layer)")
        ax.legend(fontsize=8)
        ax.grid(True, axis="y", alpha=0.3)
        save(fig, "b2_read_ops.png")

        # --- figure 3: latency change and rtt per dependent read (mmap) ---
        fig, axes = plt.subplots(2, 1, figsize=(9, 6.5))
        dp50 = np.array([100 * r["latency_delta_pct_p50"] for r in statics])
        axes[0].bar(x, dp50, color="tab:red")
        axes[0].axhline(0, color="black", linewidth=0.8)
        axes[0].set_xticks(x)
        axes[0].set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        axes[0].set_ylabel("p50 latency delta (%)")
        axes[0].set_title("Baseline 2 latency change (mmap: syscall, NOT a "
                          "round-trip cost - RDMA required for the real number)")
        axes[0].grid(True, axis="y", alpha=0.3)
        rtt = np.array([r["rtt_per_dep_read_us"] for r in statics])
        axes[1].bar(x, rtt, color="tab:orange")
        axes[1].axhline(0, color="black", linewidth=0.8)
        axes[1].set_xticks(x)
        axes[1].set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        axes[1].set_ylabel("us per dependent read")
        axes[1].set_title("Apparent cost of one extra dependent read (mmap)")
        axes[1].grid(True, axis="y", alpha=0.3)
        save(fig, "b2_latency_mmap.png")

    # --- figure 4: saving share vs update ratio (dynamic requirement) ------
    mixed = [r for r in recs if r["update_op"] in ("erase", "upsert")]
    if mixed:
        fig, axes = plt.subplots(1, 2, figsize=(11, 4.5), sharey=True)
        for ax, op in zip(axes, ("erase", "upsert")):
            series = defaultdict(list)
            for r in mixed:
                if r["update_op"] != op:
                    continue
                size = r["label"].rsplit("_", 2)[0]
                key = f"n={r['label'].split('_')[0][1:]} d={r['dim']} M={r['M']}"
                series[key].append((r["ratio"], 100 * r["saving_pct_total"]))
            for key, pts in sorted(series.items()):
                pts.sort()
                ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o",
                        label=key)
            ax.set_xlabel("update ratio")
            ax.set_title(f"{op}: saved bytes as % of total read bytes")
            ax.grid(True, alpha=0.3)
            ax.legend(fontsize=7)
        axes[0].set_ylabel("saved_bytes / total bytes (%)")
        save(fig, "b2_saving_vs_update_ratio.png")

    # --- figure 5: ops penalty vs capacity utilisation (model) -------------
    if len(recs) > 1:
        fig, ax = plt.subplots(figsize=(7, 4.5))
        cx = [r["b1_cap_util"] for r in recs]
        cy = [100 * r["ops_penalty_pct"] for r in recs]
        ax.scatter(cx, cy, s=18)
        ax.set_xlabel("capacity utilization (mean degree / capacity, B1)")
        ax.set_ylabel("extra neighbour read ops per B1 op (%)")
        ax.set_title("Baseline 2 op penalty: one extra read per (non-empty) layer")
        ax.grid(True, alpha=0.3)
        save(fig, "b2_ops_penalty_vs_util.png")

    # --- degree histograms (structural context) ---------------------------
    for tag, hist in hist_entries:
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
        fig.suptitle(f"Degree histogram by level - {tag}")
        save(fig, f"degree_hist_{tag}.png")

    return written


# --------------------------------------------------------------------------- #
def parse_args(argv):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--summary", type=Path, required=True)
    p.add_argument("--index", type=Path, action="append", default=[],
                   help="RHNSW index to inspect (repeatable)")
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--report", type=Path, default=None)
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    rows = read_summary(args.summary)
    pairs = pair_rows(rows)
    if not pairs:
        print("no Baseline 1 / Baseline 2 pairs found in the summary",
              file=sys.stderr)
        return 2
    recs, violations = compute(pairs)

    print(render_table(recs))
    print()
    print(model_notes(recs))
    print()
    print(dynamic_notes(recs))
    if violations:
        print("\nIDENTITY VIOLATIONS:")
        for v in violations:
            print(f"  {v}")
    else:
        print("\nall Baseline 2 identities hold")

    report = ["# Baseline 2 analysis\n",
              "## per-config deltas\n", "```", render_table(recs), "```", "",
              "## model checks\n", "```", model_notes(recs), "```", "",
              "## dynamic workload\n", "```", dynamic_notes(recs), "```", ""]
    report.append("## identity checks\n")
    if violations:
        report.extend(f"- VIOLATION: {v}\n" for v in violations)
    else:
        report.append("- all Baseline 2 identities hold "
                      "(sections 8.3 (1)(2)(3)(4))\n")

    for path in args.index:
        info = degree_histogram(path)
        print(f"\n{path}")
        print(format_histogram(info))
        report.append(f"## degree histogram: {path}\n\n```\n"
                      f"{format_histogram(info)}\n```\n")

    if args.out_dir is not None:
        hist_entries = [(p.stem, degree_histogram(p)) for p in args.index]
        written = make_charts(recs, hist_entries, args.out_dir)
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
