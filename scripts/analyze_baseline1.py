#!/usr/bin/env python3
"""Analysis and charts for the Baseline 1 experiments (T8).

Two independent inputs:

  * ``--summary`` : the summary CSV produced by ``baseline1_experiment``
    (one row per run), used for the waste-ratio / latency / neighbor-share
    figures.
  * ``--index``   : an RHNSW ``.bin`` index, parsed directly to build the
    per-level neighbour-degree histogram.  This is the figure the plan says to
    look at *first*: if the base layer is almost always full, the static
    Baseline 1 waste is tiny and the motivation must come from dynamic,
    high-level or low-dimensional settings.

The RHNSW layout parsed here matches src/graph/graph_store.cc:

    IndexHeader (64B, system V alignment)
    Offset[node_count] (uint64)
    per node: DiskNodeHeader (24B) + float32[dim]
              + for level 0..max_level: uint32 degree + NodeId slot[capacity]
      capacity(0) = 2*M,  capacity(l>0) = M

The record size is identical for every node (``serialized_node_size`` ignores
the node contents), which lets the parser read degrees with strided gathers.

Usage
-----
    python3 scripts/analyze_baseline1.py \
        --summary out/baseline1_summary.csv \
        --index data/index_10000.bin \
        --out-dir out/figures --report out/baseline1_report.md
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HEADER = np.dtype([
    ("magic", "S8"),
    ("version", "<u4"),
    ("dim", "<u4"),
    ("M", "<u4"),
    ("max_level", "<u4"),
    ("entrypoint", "<u4"),
    ("node_count", "<u8"),
    ("generation", "<u8"),
    ("offsets_begin", "<u8"),
    ("records_begin", "<u8"),
], align=True)

NODE_HEADER = np.dtype([
    ("id", "<u4"),
    ("generation", "<u8"),
    ("level", "<u4"),
    ("deleted", "u1"),
    ("pad", "u1", 3),
], align=True)


# --------------------------------------------------------------------------- #
# index parsing
# --------------------------------------------------------------------------- #
def read_index_header(path: Path) -> dict:
    with open(path, "rb") as f:
        raw = f.read(HEADER.itemsize)
    hdr = np.frombuffer(raw, dtype=HEADER, count=1)[0]
    if bytes(hdr["magic"]) != b"RHNSW001":
        raise ValueError(f"{path} is not an RHNSW index (bad magic)")
    return {
        "dim": int(hdr["dim"]),
        "M": int(hdr["M"]),
        "max_level": int(hdr["max_level"]),
        "node_count": int(hdr["node_count"]),
        "offsets_begin": int(hdr["offsets_begin"]),
    }


def layer_capacity(M: int, level: int) -> int:
    return 2 * M if level == 0 else M


def degree_histogram(path: Path) -> dict:
    info = read_index_header(path)
    dim, M, max_level = info["dim"], info["M"], info["max_level"]
    nc, offsets_begin = info["node_count"], info["offsets_begin"]

    caps = [layer_capacity(M, l) for l in range(max_level + 1)]
    level_offsets = []
    cursor = NODE_HEADER.itemsize + dim * 4
    for cap in caps:
        level_offsets.append(cursor)
        cursor += 4 + cap * 4
    record_size = cursor

    mm = np.memmap(path, dtype=np.uint8, mode="r")
    offsets = np.frombuffer(
        np.asarray(mm[offsets_begin:offsets_begin + 8 * nc]), dtype="<u8")

    def gather_u32(off: int) -> np.ndarray:
        pos = offsets + off
        cols = [mm[pos + k] for k in range(4)]
        return np.ascontiguousarray(np.stack(cols, axis=1)).view("<u4").ravel()

    def gather_u8(off: int) -> np.ndarray:
        return np.asarray(mm[offsets + off])

    node_level = gather_u32(16)          # DiskNodeHeader.level
    node_deleted = gather_u8(20) != 0

    per_level = {}
    for level, off in enumerate(level_offsets):
        cap = caps[level]
        degree = np.minimum(gather_u32(off).astype(np.int64), cap)
        present = (node_level >= level) & (~node_deleted)
        per_level[level] = {
            "capacity": cap,
            "nodes": int(present.sum()),
            "degree": degree[present],
        }

    return {
        "dim": dim, "M": M, "max_level": max_level, "node_count": nc,
        "record_size": record_size, "per_level": per_level,
        "deleted": int(node_deleted.sum()),
    }


def format_histogram(info: dict) -> str:
    lines = [
        f"index: node_count={info['node_count']} dim={info['dim']} "
        f"M={info['M']} max_level={info['max_level']} "
        f"record_size={info['record_size']}B deleted={info['deleted']}",
        "",
        "level  nodes    capacity  mean_deg  mean_util  full%   "
        "degree histogram (deg:count, sparse->full)",
    ]
    for level, st in info["per_level"].items():
        deg, cap, nodes = st["degree"], st["capacity"], st["nodes"]
        if nodes == 0:
            lines.append(f"{level:>5}  {nodes:>8}")
            continue
        values, counts = np.unique(deg, return_counts=True)
        hist = " ".join(f"{int(v)}:{int(c)}" for v, c in zip(values, counts))
        lines.append(
            f"{level:>5}  {nodes:>8}  {cap:>8}  {deg.mean():>8.2f}  "
            f"{deg.mean() / cap:>9.3f}  "
            f"{100.0 * (deg == cap).mean():>5.1f}  {hist}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# summary CSV
# --------------------------------------------------------------------------- #
def parse_label(label: str) -> tuple[str, str, float]:
    parts = label.split("_")
    size = parts[0]
    if len(parts) >= 2 and parts[1] == "static":
        return size, "static", 0.0
    if len(parts) >= 3:
        try:
            return size, parts[1], float(parts[2])
        except ValueError:
            pass
    return size, "unknown", 0.0


def read_summary(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["size"], r["op"], r["ratio"] = parse_label(r["label"])
        for key in ("qps", "latency_p50_us", "latency_p95_us", "latency_p99_us",
                    "waste_ratio", "neighbor_share", "capacity_utilization",
                    "avg_degree", "recall"):
            r[key] = float(r[key])
        for key in ("requested_bytes", "useful_bytes", "wasted_bytes",
                    "node_bytes", "read_ops", "layer_reads"):
            r[key] = int(r[key])
    return rows


def render_tables(rows: list[dict]) -> str:
    cols = ["dataset", "label", "queries", "qps", "latency_p50_us",
            "latency_p95_us", "latency_p99_us", "waste_ratio",
            "capacity_utilization", "avg_degree", "neighbor_share", "recall"]
    header = " | ".join(cols)
    lines = [header, "-" * len(header)]
    for r in rows:
        cells = []
        for c in cols:
            v = r[c]
            if isinstance(v, float):
                cells.append(f"{v:.4f}")
            else:
                cells.append(str(v))
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def neighbor_share_model(rows: list[dict]) -> str:
    """Compare the measured neighbor-region share with 1/(dim+6)."""
    lines = ["neighbor-share model check  (measured vs approx 1/(dim+6)):"]
    seen = {}
    for r in rows:
        dim = int(r["dim"])
        seen.setdefault(dim, []).append(r["neighbor_share"])
    for dim in sorted(seen):
        measured = float(np.mean(seen[dim]))
        model = 1.0 / (dim + 6.0)
        lines.append(f"  dim={dim:>4}: measured={measured:.5f}  "
                     f"model 1/(dim+6)={model:.5f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# charts (optional; requires matplotlib)
# --------------------------------------------------------------------------- #
def make_charts(rows: list[dict], hist_entries: list[tuple[str, dict]],
                out_dir: Path) -> list[str]:
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

    if rows:
        # waste ratio vs update ratio, one line per (dataset+size, op)
        series = defaultdict(list)
        for r in rows:
            group = f'{r["dataset"]} {r["size"]}'
            series[(group, r["op"])].append((r["ratio"], r["waste_ratio"]))
        fig, ax = plt.subplots(figsize=(7, 4.5))
        for (group, op), pts in sorted(series.items()):
            pts.sort()
            x = [p[0] for p in pts]
            y = [p[1] for p in pts]
            ax.plot(x, y, marker="o", label=f"{group} {op}")
        ax.set_xlabel("update ratio")
        ax.set_ylabel("waste ratio  (wasted / requested, neighbor region)")
        ax.set_title("Baseline 1 neighbor-region waste vs update ratio")
        ax.set_ylim(bottom=0.0)
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        save(fig, "waste_vs_update_ratio.png")

        # capacity utilization vs update ratio
        fig, ax = plt.subplots(figsize=(7, 4.5))
        for group, op in sorted(series):
            pts = [(r["ratio"], r["capacity_utilization"]) for r in rows
                   if f'{r["dataset"]} {r["size"]}' == group and r["op"] == op]
            pts.sort()
            ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o",
                    label=f"{group} {op}")
        ax.set_xlabel("update ratio")
        ax.set_ylabel("capacity utilization  (mean degree / capacity)")
        ax.set_title("Baseline 1 capacity utilization vs update ratio")
        ax.set_ylim(0.0, 1.0)
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        save(fig, "utilization_vs_update_ratio.png")

        # neighbor-region share of total bytes
        fig, ax = plt.subplots(figsize=(7, 4.5))
        labels = [r["label"] for r in rows]
        share = [100.0 * r["neighbor_share"] for r in rows]
        ax.bar(range(len(rows)), share)
        ax.set_xticks(range(len(rows)))
        ax.set_xticklabels(labels, rotation=90, fontsize=7)
        ax.set_ylabel("neighbor region as % of all requested bytes")
        ax.set_title("Neighbor-region share of total RDMA-read bytes")
        ax.grid(True, axis="y", alpha=0.3)
        save(fig, "neighbor_share.png")

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
                f"level {level}: capacity {st['capacity']}, "
                f"{st['nodes']} nodes, "
                f"util {st['degree'].mean() / st['capacity']:.3f}", fontsize=9)
            ax.legend(fontsize=8)
        fig.suptitle(f"Degree histogram by level - {tag}")
        save(fig, f"degree_hist_{tag}.png")

    # Level-0 comparison across indices (the key structural figure).
    level0 = [(tag, hist["per_level"].get(0)) for tag, hist in hist_entries
              if hist["per_level"].get(0)
              and hist["per_level"][0]["nodes"] > 0]
    if len(level0) >= 2:
        fig, ax = plt.subplots(figsize=(7.5, 4.5))
        for tag, st in level0:
            ax.hist(st["degree"], bins=range(0, st["capacity"] + 2),
                    align="left", rwidth=0.8, alpha=0.5,
                    label=f"{tag} (cap {st['capacity']})")
        ax.set_xlabel("neighbor degree")
        ax.set_ylabel("nodes")
        ax.set_title("Level-0 degree histogram comparison")
        ax.legend(fontsize=8)
        save(fig, "degree_hist_level0_compare.png")

    return written


# --------------------------------------------------------------------------- #
def parse_args(argv):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--summary", type=Path, default=None)
    p.add_argument("--index", type=Path, action="append", default=[],
                   help="RHNSW index to inspect (repeatable)")
    p.add_argument("--out-dir", type=Path, default=None,
                   help="directory for PNG figures")
    p.add_argument("--report", type=Path, default=None,
                   help="write a markdown report to this path")
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    if args.summary is None and not args.index:
        print("nothing to do: pass --summary and/or --index", file=sys.stderr)
        return 2

    report = []
    rows = []
    hist_infos = []

    if args.summary is not None:
        rows = read_summary(args.summary)
        report.append("# Baseline 1 summary\n")
        report.append(render_tables(rows))
        report.append("")
        report.append(neighbor_share_model(rows))
        report.append("")
        print(render_tables(rows))
        print()
        print(neighbor_share_model(rows))

    for path in args.index:
        info = degree_histogram(path)
        hist_infos.append((path, info))
        text = f"## degree histogram: {path}\n\n```\n{format_histogram(info)}\n```\n"
        report.append(text)
        print(f"\n{path}")
        print(format_histogram(info))

    if args.out_dir is not None:
        hist_entries = [(p.stem, info) for p, info in hist_infos]
        written = make_charts(rows, hist_entries, args.out_dir)
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
