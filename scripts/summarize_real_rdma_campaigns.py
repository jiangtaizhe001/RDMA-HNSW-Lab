#!/usr/bin/env python3
"""Consolidate every real-data campaign into one markdown summary.

Each campaign directory produced by ``run_real_rdma_campaign.py`` carries an
``analysis/`` folder.  This script reads those folders and writes a single
report-ready document so numbers are never retyped by hand:

  * quality gate (independent split) per campaign,
  * timed window per cell (static / capacity / seed runs) with the paired ratio
    against B3,
  * concurrency: common-window aggregate QPS, pooled tail latency, Jain index
    and the overlap check,
  * mixed: query-window QPS, update latency, end-to-end throughput,
  * checkpoint quality per update count.

Usage:
    python3 scripts/summarize_real_rdma_campaigns.py \
        --out-dir out/real_data_rdma --output out/real_data_rdma/summary_all.md
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_csv(path: Path) -> list[dict]:
    if not path.exists() or not path.stat().st_size:
        return []
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def number(row: dict, name: str, default: float = float("nan")) -> float:
    value = row.get(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def timed_section(campaign: Path) -> list[str]:
    aggregate = read_csv(campaign / "analysis/aggregate.csv")
    paired = {(row["cell"], row["config"], row["metric"]): row
              for row in read_csv(campaign / "analysis/paired.csv")}
    if not aggregate:
        return []
    lines = ["| cell | config | reps | p50 µs | p95 µs | QPS | READ/q | B/q | "
             "waste | hit | mean vs B3 [95% CI] |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in aggregate:
        reference = paired.get((row["cell"], row["config"], "timed_mean_us"))
        ratio = (f"{number(reference, 'ratio_median'):.4f} "
                 f"[{number(reference, 'ci_low'):.4f}, "
                 f"{number(reference, 'ci_high'):.4f}]"
                 if reference else "—")
        lines.append(
            f"| {row['cell']} | {row['config']} | {row['reps']} | "
            f"{number(row, 'timed_p50_us'):.0f} | "
            f"{number(row, 'timed_p95_us'):.0f} | "
            f"{number(row, 'timed_qps'):.1f} | "
            f"{number(row, 'timed_reads_per_query'):.2f} | "
            f"{number(row, 'timed_requested_bytes_per_query'):.0f} | "
            f"{number(row, 'timed_waste_ratio'):.3f} | "
            f"{number(row, 'timed_cache_hit_rate'):.3f} | {ratio} |")
    return lines + [""]


def concurrency_section(campaign: Path) -> list[str]:
    rows = read_csv(campaign / "analysis/concurrency.csv")
    if not rows:
        return []
    lines = ["| clients | config | groups | aggregate QPS | window s | p50 µs | "
             "p95 µs | p99 µs | Jain | overlap ok |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            f"| {row['clients']} | {row['config']} | {row['groups']} | "
            f"{number(row, 'aggregate_qps'):.1f} | "
            f"{number(row, 'window_s'):.1f} | "
            f"{number(row, 'pooled_p50_us'):.0f} | "
            f"{number(row, 'pooled_p95_us'):.0f} | "
            f"{number(row, 'pooled_p99_us'):.0f} | "
            f"{number(row, 'jain_fairness'):.4f} | {row.get('overlap_ok')} |")
    return lines + [""]


def mixed_section(campaign: Path) -> list[str]:
    """Per-cell query window plus the update cost of the same runs."""
    aggregate = read_csv(campaign / "analysis/aggregate.csv")
    rows = [row for row in aggregate if row.get("phase") == "mixed"]
    if not rows:
        return []
    lines = ["| cell | config | reps | p50 µs | QPS | update ms | update p95 ms | "
             "end-to-end QPS | updates | failed | READ/q | B/q |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(
            f"| {row['cell']} | {row['config']} | {row['reps']} | "
            f"{number(row, 'timed_p50_us'):.0f} | "
            f"{number(row, 'timed_qps'):.1f} | "
            f"{number(row, 'run_update_latency_mean_us') / 1000:.1f} | "
            f"{number(row, 'run_update_latency_p95_us') / 1000:.1f} | "
            f"{number(row, 'run_qps_end_to_end'):.1f} | "
            f"{number(row, 'run_updates'):.0f} | "
            f"{number(row, 'run_update_failures'):.0f} | "
            f"{number(row, 'timed_reads_per_query'):.2f} | "
            f"{number(row, 'timed_requested_bytes_per_query'):.0f} |")
    return lines + [""]


def checkpoint_section(campaign: Path) -> list[str]:
    rows = read_csv(campaign / "analysis/checkpoint.csv")
    if not rows:
        return []
    grouped: dict = {}
    for row in rows:
        grouped.setdefault((row["op"], float(row["ratio"]),
                            int(row["cumulative_updates"])), {}) \
            .setdefault(row["config"], []).append(float(row["recall"]))
    lines = ["| op | spacing | updates | " + " | ".join(
        sorted({row["config"] for row in rows}, key=lambda c: (c[0], len(c), c))) + " |"]
    configs = sorted({row["config"] for row in rows}, key=lambda c: (c[0], len(c), c))
    lines.append("| --- | --- | --- | " + " | ".join("---" for _ in configs) + " |")
    for (op, ratio, count), by_config in sorted(grouped.items()):
        cells = []
        for config in configs:
            values = by_config.get(config, [])
            cells.append(f"{sum(values) / len(values):.4f}" if values else "—")
        lines.append(f"| {op} | {ratio} | {count} | " + " | ".join(cells) + " |")
    return lines + [""]


def quality_section(campaign: Path) -> list[str]:
    rows = read_csv(campaign / "analysis/quality.csv")
    if not rows:
        return []
    by_cell: dict = {}
    for row in rows:
        by_cell.setdefault(row["cell"], []).append(row)
    lines = []
    for cell, members in sorted(by_cell.items()):
        recalls = sorted({round(float(row["recall"]), 4) for row in members})
        failed = sum(int(row["failed_reads"]) for row in members)
        lines.append(f"- `{cell}`: recall {', '.join(f'{value:.4f}' for value in recalls)}"
                     f"（{len(members)} 配置，读取失败 {failed}）")
    return lines + [""]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=Path("out/real_data_rdma"))
    parser.add_argument("--output", type=Path,
                        default=Path("out/real_data_rdma/summary_all.md"))
    args = parser.parse_args(argv)

    campaigns = sorted(path for path in args.out_dir.iterdir()
                       if path.is_dir() and (path / "analysis").is_dir())
    lines = ["# 真实数据 RDMA 活动汇总（自动生成）", "",
             f"共 {len(campaigns)} 个活动，源自各活动 `analysis/` 目录。"
             "比值为相对 B3 的按轮配对 bootstrap 95% 区间；"
             "反复运行的同一活动以最后一次分析为准。", ""]
    for campaign in campaigns:
        manifest = campaign / "manifest.json"
        header = [f"## {campaign.name}", ""]
        if manifest.exists():
            data = json.loads(manifest.read_text())
            identity = data.get("identity", {})
            header.append(f"- 数据集 `{identity.get('dataset')}`，n={identity.get('n')}，"
                          f"k={identity.get('k')}，缓存 {identity.get('cache_capacity')}，"
                          f"预算 {identity.get('budgets')}，重复 {identity.get('reps')}，"
                          f"传输 {identity.get('transport')}")
            header.append("")
        sections = []
        quality = quality_section(campaign)
        if quality:
            sections += ["### 质量门禁（独立划分）", ""] + quality
        timed = timed_section(campaign)
        if timed:
            sections += ["### 计时窗口", ""] + timed
        concurrency = concurrency_section(campaign)
        if concurrency:
            sections += ["### 并发（共同窗口）", ""] + concurrency
        mixed = mixed_section(campaign)
        if mixed:
            sections += ["### 混合 workload", ""] + mixed
        checkpoints = checkpoint_section(campaign)
        if checkpoints:
            sections += ["### 检查点质量（当前状态精确 GT）", ""] + checkpoints
        if not sections:
            header.append("（尚无分析产物）")
            header.append("")
        lines += header + sections
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n")
    print(f"wrote {args.output} ({len(campaigns)} campaigns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
