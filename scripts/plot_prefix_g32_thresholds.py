#!/usr/bin/env python3
"""Plot N versus expansion-budget midpoint estimates and measured brackets."""
import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from run_prefix_g32_efsearch import ROOT, SEMANTICS, digest, publish


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    args = parser.parse_args()
    campaign = args.campaign.resolve()
    source = campaign / "analysis/thresholds.csv"
    done = json.loads((campaign / "ANALYSIS_DONE.json").read_text())
    if done["exit_code"] != 0 or digest(source) != done["outputs"]["analysis/thresholds.csv"]:
        raise ValueError("threshold input is not verified")
    with source.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    figure, axis = plt.subplots(figsize=(11, 7))
    for target, color, offset in ((0.95, "#007f86", -18), (0.99, "#bd3f35", 12)):
        points = sorted((row for row in rows if float(row["target"]) == target), key=lambda row: int(row["N"]))
        if len(points) != 8 or len({row["N"] for row in points}) != 8:
            raise ValueError("expected eight unique sizes per target")
        sizes = [int(row["N"]) for row in points]
        estimates = [int(row["estimate"]) for row in points]
        lower = [int(row["estimate"]) - int(row["L"]) for row in points]
        upper = [int(row["R"]) - int(row["estimate"]) for row in points]
        if any(row["search_semantics"] != SEMANTICS for row in points) or min(lower + upper) < 0:
            raise ValueError("invalid semantics or bracket")
        axis.errorbar(sizes, estimates, yerr=[lower, upper], color=color, marker="o", markersize=5,
                      linewidth=1.8, capsize=4, label=f"recall@10 target {target:.2f}")
        for size, estimate in zip(sizes, estimates):
            axis.annotate(f"{estimate:,}", (size, estimate), xytext=(0, offset), textcoords="offset points",
                          ha="center", fontsize=9, color=color)
    axis.set(xscale="log", yscale="log", xlabel="Index node count N (log scale)",
             ylabel="efsearch midpoint estimate (expansion budget, log scale)",
             title="G=32: index size versus efsearch")
    axis.set_xticks([1000, 2000, 5000, 10000, 20000, 50000, 100000, 1000000],
                      ["1K", "2K", "5K", "10K", "20K", "50K", "100K", "1M"])
    axis.grid(which="major", alpha=0.25)
    axis.grid(which="minor", alpha=0.08)
    axis.legend(loc="upper left")
    axis.margins(x=0.07, y=0.15)
    figure.text(0.5, 0.035, "prototype_expansion_budget | markers: midpoint estimates; error bars: measured (L, R] brackets",
                ha="center", fontsize=9)
    figure.text(0.5, 0.012, "Midpoints are not guaranteed to meet the recall target. Pilot runs are timing calibration only.",
                ha="center", fontsize=9)
    figure.tight_layout(rect=(0, 0.065, 1, 1))
    output = ROOT / "out/prefix_g32_efsearch/n_efsearch_trend.png"
    figure.savefig(output, dpi=200)
    plt.close(figure)
    publish(output.with_suffix(".json"), dict(source=str(source), source_sha256=digest(source),
            image_sha256=digest(output), search_semantics=SEMANTICS, axis_scales="log-log",
            description="Midpoint estimates with measured brackets; not pilot-derived thresholds."))
    print(output)


if __name__ == "__main__":
    main()