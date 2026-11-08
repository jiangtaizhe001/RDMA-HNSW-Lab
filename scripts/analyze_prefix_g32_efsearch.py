#!/usr/bin/env python3
"""Validate completed G=32 runs and publish curves, threshold evidence and report."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from run_prefix_g32_efsearch import (COARSE, ROOT, SEMANTICS, SIZES, digest,
                                     publish, validate)


def classify(size, points, target):
    primary = {ef: values[0] for ef, values in points.items() if 0 in values}
    ordered = sorted(primary)
    coarse = {ef for ef in COARSE if ef <= size} | {size}
    missing = sorted(coarse - primary.keys())
    result = dict(N=size, target=target / 10000, ef="", previous_recall="",
                  recall="", status="incomplete", missing_coarse=";".join(map(str, missing)))
    if any(len(set(values.values())) != 1 or 0 not in values for values in points.values()):
        return dict(result, status="repeat_conflict")
    if any(primary[left] > primary[right] for left, right in zip(ordered, ordered[1:])):
        return dict(result, status="nonmonotonic")
    passed = [ef for ef in ordered if primary[ef] >= target]
    if not passed:
        return dict(result, status="unreached" if size in primary else "incomplete",
                    max_ef=max(ordered, default=""),
                    max_recall=max(primary.values(), default=0) / 10000)
    threshold = passed[0]
    result.update(ef=threshold, recall=primary[threshold] / 10000)
    if threshold > 10 and threshold - 1 in primary:
        result["previous_recall"] = primary[threshold - 1] / 10000
    boundaries = [threshold] if threshold == 10 else [threshold - 1, threshold]
    confirmed = all(set(points.get(ef, {})) == {0, 1, 2} for ef in boundaries)
    bracket = threshold == 10 or (threshold - 1 in primary and primary[threshold - 1] < target)
    if not missing and confirmed and bracket:
        result["status"] = "exact_under_verified_monotonicity"
    else:
        result["status"] = "minimum_measured_only"
    return result


def self_test():
    size = 10000
    points = {ef: {0: (9400 if ef < 100 else 9950)}
              for ef in sorted({ef for ef in COARSE if ef <= size} | {size})}
    points.update({99: {0: 9499, 1: 9499, 2: 9499},
                   100: {0: 9500, 1: 9500, 2: 9500}})
    assert classify(size, points, 9500)["status"] == "exact_under_verified_monotonicity"
    missing = {ef: value for ef, value in points.items() if ef != size}
    assert classify(size, missing, 9500)["status"] == "minimum_measured_only"
    assert classify(size, {size: {0: 9400}}, 9500)["status"] == "unreached"
    assert classify(size, {64: {0: 9400}}, 9500)["status"] == "incomplete"
    assert classify(size, {64: {0: 9500}, 128: {0: 9400}}, 9500)["status"] == "nonmonotonic"
    assert classify(size, {64: {0: 9500, 1: 9499}}, 9500)["status"] == "repeat_conflict"
    edge = {ef: {0: 9900} for ef in points}
    edge[10] = {0: 9900, 1: 9900, 2: 9900}
    assert classify(size, edge, 9900)["ef"] == 10
    assert classify(size, edge, 9900)["status"] == "exact_under_verified_monotonicity"
    print("threshold exact equality, missing points, unreachable, timeout, monotonicity, repeats, ef=10: PASS")


def write_csv(path, rows):
    fields = list(dict.fromkeys(field for row in rows for field in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def analyze(args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    manifest = json.loads((args.out / "manifest.json").read_text())
    prepared = json.loads((args.data / "prepared.json").read_text())
    if manifest["identity"] != prepared["identity"]:
        raise ValueError("manifest/input identity conflict")
    for name, sha in manifest["identity"]["files"].items():
        if digest(ROOT / name) != sha:
            raise ValueError(f"script/binary changed: {name}")
    for name, sha in prepared["hashes"].items():
        if digest(args.data / name) != sha:
            raise ValueError(f"input changed: {name}")
    curves, thresholds = [], []
    all_points = {}
    for size in SIZES:
        directory = args.out / f"n{size}"
        points = {}
        for marker in sorted((directory / "completed").glob("*.json")):
            saved = json.loads(marker.read_text())
            key = saved["key"]
            ef, repeat = key["ef"], key["repeat"]
            if (key["identity"] != manifest["identity"] or key["N"] != size or
                    key["count"] != 1000 or not 10 <= ef <= size or repeat not in (0, 1, 2) or
                    saved["exit_code"] != 0):
                raise ValueError(f"invalid completed run: {marker}")
            expected_inputs = {name: prepared["hashes"][name] for name in
                               (f"index{size}.bin", "queries.fvecs", f"gt{size}.u32")}
            if key["inputs"] != expected_inputs:
                raise ValueError("run input identity mismatch")
            label = marker.stem
            summary = directory / "summary" / f"{label}.csv"
            per_query = directory / "per_query" / f"{label}.csv"
            expected_outputs = {str(path.relative_to(directory)): digest(path)
                                for path in (summary, per_query)}
            if expected_outputs != saved["outputs"]:
                raise ValueError("completed output hash mismatch")
            row = validate(summary, per_query, size, ef, 1000)
            if repeat in points.setdefault(ef, {}):
                raise ValueError("duplicate CSV/run identity")
            points[ef][repeat] = row["hits"]
            curves.append(dict(N=size, ef=ef, ef_over_N=ef / size, repeat=repeat,
                               phase=saved["phase"], search_semantics=SEMANTICS,
                               **row))
        all_points[size] = points
        thresholds.extend(classify(size, points, target) for target in (9500, 9900))
    write_csv(args.out / "curve.csv", curves)
    write_csv(args.out / "thresholds.csv", thresholds)
    figures = args.out / "figures"
    figures.mkdir(exist_ok=True)
    for normalized in (False, True):
        figure, axis = plt.subplots(figsize=(9, 5))
        for size, points in all_points.items():
            primary = sorted((ef, values[0] / 10000) for ef, values in points.items() if 0 in values)
            if not primary:
                continue
            axis.plot([ef / size if normalized else ef for ef, _ in primary],
                      [recall for _, recall in primary], marker=".", label=f"N={size}")
            for item in thresholds:
                if item["N"] == size and item["ef"] != "":
                    axis.scatter(item["ef"] / size if normalized else item["ef"], item["recall"], marker="x")
        for target in (0.95, 0.99):
            axis.axhline(target, linestyle="--", color="gray", linewidth=0.8)
        axis.set(xlabel="efsearch / N (expansion budget)" if normalized else "efsearch (expansion budget)",
                 ylabel="recall@10", ylim=(0, 1.01), title=SEMANTICS)
        if curves:
            axis.legend()
        axis.grid(alpha=0.2)
        figure.tight_layout()
        figure.savefig(figures / ("recall_ef_over_N.png" if normalized else "recall_ef.png"), dpi=160)
        plt.close(figure)
    estimates_path = args.out / "pilot/estimates.json"
    estimates = json.loads(estimates_path.read_text()) if estimates_path.exists() else {}
    lines = ["# G=32 四规模 efsearch-recall 实验报告", "",
             "本报告仅依据已校验完成标记生成，未执行的点不计为 recall=0。", "",
             f"运行状态：{manifest['status']}；阶段：{manifest['phase']}。",
             f"受阻原因：{manifest.get('reason', '无')}。", "",
             "## 固定协议", "",
             "搜索语义：`search_semantics=prototype_expansion_budget`。efsearch 是扩展节点预算，"
             "不是标准 HNSW 的候选束宽。没有修改 C++ 或搜索语义。",
             "四规模独立构图，128 维均匀合成数据；base seed=42，M=16，ef_construction=200；"
             "共用 seed=45 的 1000 条查询，k=10，mmap，G=32，cache_capacity=100000，warmup=0。",
             "每次客户端是独立进程、空缓存。mmap 成本不能解释为 RDMA 性能。", "",
             "## 真值与来源", "",
             "修复旧 GT 的整数数值转换错误，按小端 float32 还原载荷；严格校验各记录维度、长度、"
             "有限值、生成范围和直接解包值。四规模独立使用 float64 分块距离与稳定节点 ID 排序，"
             "各抽查固定 10 条查询，独立直接差平方和结果一致。旧 GT 和历史 recall 不可直接比较。",
             f"GT 抽检及第 10/11 名并列记录：`{args.data / 'prepared.json'}`。",
             f"Python={manifest['python']}，NumPy={manifest['numpy']}，Matplotlib={manifest['matplotlib']}。",
             f"代码版本：`{manifest['git_head']}`；二进制、脚本及输入 SHA-256 见 manifest 和 prepared 清单。", "",
             "## 实测阈值", "",
             "| N | ef@0.95 | recall(e-1) / recall(e) | ef@0.99 | recall(e-1) / recall(e) | 状态 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for size in SIZES:
        pair = [item for item in thresholds if item["N"] == size]
        def cell(item):
            ef = str(item["ef"]) if item["ef"] != "" else "未确定"
            boundary = f"{item['previous_recall']} / {item['recall']}" if item["recall"] != "" else "未测"
            return ef, boundary
        first, second = map(cell, pair)
        lines.append(f"| {size} | {first[0]} | {first[1]} | {second[0]} | {second[1]} | " +
                     "; ".join(item["status"] for item in pair) + " |")
    lines += ["", "`exact_under_verified_monotonicity` 表示在原型固定扩展顺序和已测非降证据下，"
              "完成粗扫、相邻整数边界与三次一致确认；`minimum_measured_only` 不声称全局最小；"
              "`unreached` 表示 ef=N 仍未达标；`incomplete` 表示预算内尚不能判断可达性。", "",
              "## 覆盖与成本", "",
              f"正式已完成点（含重复）：{len(curves)}。缺失粗扫点见 thresholds.csv 的 missing_coarse。",
              f"单点超时上限：{manifest['timeout_seconds']} 秒；本次会话预算：{manifest['budget_seconds']} 秒。",
              f"准备时磁盘余量：{manifest['disk_free_bytes']} 字节；内存快照见 manifest。",
              "pilot 固定 20 查询，仅估算成本，不进入曲线或阈值。线性估算并非运行时上限。", "",
              "| N | 粗扫估算秒数 | ef=N 估算秒数 |", "| --- | --- | --- |"]
    for size, estimate in estimates.items():
        lines.append(f"| {size} | {estimate['full_coarse_seconds']:.1f} | {estimate['ef_N_seconds']:.1f} |")
    lines += ["", "## 结果限制与产物", "",
              "阈值只适用于指定图和固定查询集，不是跨种子或真实分布保证。重复不作为独立样本。"
              "驱动不输出结果 ID 或逐查询 recall，因此不能声称保存了结果序列或逐查询置信区间。",
              f"原始摘要、逐查询成本、命令日志、完成标记、curve.csv、thresholds.csv 和 figures 均位于 `{args.out}`。",
              "曲线仅连接实测点，不使用插值认定阈值。每次运行前后检查索引哈希，分析时重新核验全部输入和完成输出。"]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines) + "\n")
    publish(args.out / "analysis.json", dict(completed_points=len(curves), thresholds=thresholds))
    print(f"validated {len(curves)} formal runs; report: {args.report}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "out/prefix_g32_efsearch")
    parser.add_argument("--data", type=Path, default=ROOT / "data/prefix_g32_efsearch")
    parser.add_argument("--report", type=Path, default=ROOT / "docs/prefix-g32-efsearch-report.md")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        analyze(args)


if __name__ == "__main__":
    main()