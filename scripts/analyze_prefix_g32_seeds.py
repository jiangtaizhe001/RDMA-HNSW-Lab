#!/usr/bin/env python3
"""Validate and summarize the five construction seeds' threshold brackets."""
import argparse
import csv
import json
from pathlib import Path

from analyze_prefix_g32_all import check_process, require
from run_prefix_g32_efsearch import ROOT, digest, publish, validate


def load(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--seed43", type=Path, required=True)
    args = parser.parse_args()
    queue, reference, seed43 = (path.resolve() for path in (args.queue, args.reference, args.seed43))
    require(load(queue / "exit.json")["exit_code"] == 0, "queue failed")
    require(load(queue / "status.json")["state"] == "completed", "queue incomplete")
    check_process(load(queue / "controller.json"))
    done = load(queue / "QUEUE_DONE.json")
    require(done["exit_code"] == 0 and set(done["campaigns"]) == {"44", "45", "46"}, "invalid queue evidence")
    reference_done = load(reference / "ANALYSIS_DONE.json")
    require(reference_done["exit_code"] == 0 and digest(reference / "manifest.json") ==
            reference_done["manifest_sha256"], "reference changed")
    baseline = load(reference / "manifest.json")
    rows, inventory, point_counts = [], {}, {}
    for size in (20000, 50000):
        original = baseline["sources"][str(size)]
        sources = {42: Path(original["campaign"]), 43: seed43,
                   **{int(seed): Path(path) for seed, path in done["campaigns"].items()}}
        for seed, campaign in sorted(sources.items()):
            manifest = load(campaign / "manifest.json")
            identity = manifest["identity"]
            require(identity.get("build_seed", 42) == seed, "seed mismatch")
            require(all(identity[key] == value for key, value in dict(G=32, k=10, queries=1000,
                    transport="mmap", cache_capacity=100000, warmup=0, base_seed=42,
                    query_seed=45, M=16, ef_construction=200,
                    semantics="prototype_expansion_budget").items()), "configuration mismatch")
            require(load(campaign / "exit.json")["exit_code"] == 0 and
                    load(campaign / "status.json")["state"] == "completed", "batch incomplete")
            require(not (campaign / "SCAN_FAILED.json").exists(), "failed batch")
            scale_done = load(campaign / f"N{size}_DONE.json")
            require(scale_done["exit_code"] == 0 and scale_done["scope"] == [size], "invalid scale completion")
            for path in [campaign / "controller.json", *campaign.glob("*process.json")]:
                check_process(load(path))
            if (campaign / "heartbeat.json").exists():
                check_process(load(campaign / "heartbeat.json")["monitor"])
            data = Path(manifest["data"])
            for name in (f"base{size}.fvecs", "queries.fvecs", f"gt{size}.u32"):
                require(manifest["inputs"][name] == original["manifest"]["inputs"][name], "input mismatch")
            for name in (f"base{size}.fvecs", "queries.fvecs", f"gt{size}.u32", f"index{size}.bin"):
                require(digest(data / name) == manifest["inputs"][name], "input changed")
                inventory[str(data / name)] = manifest["inputs"][name]
            binaries = {name: sha for name, sha in identity["files"].items() if name.startswith("build/")}
            require(binaries == {name: sha for name, sha in original["manifest"]["identity"]["files"].items()
                                 if name.startswith("build/")}, "binary mismatch")
            for name, sha in binaries.items():
                require(digest(ROOT / name) == sha, "binary changed")
            prepared = data / f"prepared{size}.json"
            command = load(prepared)["build_command"]
            require(command[command.index("--seed") + 1] == str(seed), "actual build seed mismatch")
            points = {}
            for marker in sorted(campaign.glob(f"runs/n{size}/ef*/r0/complete.json")):
                complete = load(marker)
                key = complete["key"]
                ef = key["ef"]
                require(key["identity"] == identity and key["N"] == size and key["count"] == 1000 and
                        key.get("repeat", 0) == 0 and complete["exit_code"] == 0, "invalid point")
                require(key["inputs"] == {name: manifest["inputs"][name] for name in
                        (f"index{size}.bin", "queries.fvecs", f"gt{size}.u32")}, "point input mismatch")
                for name, sha in complete["outputs"].items():
                    require(digest(marker.parent / name) == sha, "output changed")
                    inventory[str(marker.parent / name)] = sha
                points[ef] = validate(marker.parent / "summary.csv", marker.parent / "per_query.csv",
                                      size, ef, 1000)["hits"]
                inventory[str(marker)] = digest(marker)
            ordered = sorted(points)
            require(all(points[left] <= points[right] for left, right in zip(ordered, ordered[1:])),
                    "nonmonotonic recall")
            point_counts[f"n{size}/seed{seed}"] = len(points)
            for target in (9500, 9900):
                path = campaign / f"targets/n{size}/recall{target // 100:03d}.json"
                result = load(path)
                left, right, middle = result["L"], result["R"], result["estimate"]
                require(result["identity"] == identity and result["state"] == "estimated" and
                        result["stopping_width"] == 4, "invalid target")
                require(0 < right - left <= 4 and middle == (left + right) // 2, "invalid interval")
                require(points[left] < target <= points[right], "invalid bracket")
                require(all(result[field] == points[ef] / 10000 for field, ef in
                            (("recall_L", left), ("recall_R", right), ("recall", middle))), "recall mismatch")
                require(result["midpoint_passes"] == (points[middle] >= target), "midpoint mismatch")
                require(scale_done["evidence"][str(path.relative_to(campaign))] == digest(path), "evidence changed")
                rows.append(dict(N=size, seed=seed, target=target / 10000, L=left, R=right,
                    recall_L=result["recall_L"], recall_R=result["recall_R"], width=right - left,
                    estimate=middle, midpoint_recall=result["recall"], midpoint_passes=result["midpoint_passes"],
                    campaign=str(campaign)))
                inventory[str(path)] = digest(path)
            for path in (campaign / "manifest.json", campaign / "exit.json", campaign / f"N{size}_DONE.json", prepared):
                inventory[str(path)] = digest(path)
    require(len(rows) == 20, "missing target")
    output = queue / "analysis"
    output.mkdir(exist_ok=True)
    with (output / "thresholds_by_seed.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# 20K / 50K 五组构图种子阈值区间汇总", "",
        "构图种子为 42、43、44、45、46；base seed=42、query seed=45，固定 1000 条查询。",
        "同规模 base/query/GT 哈希与实验二进制一致；沿用 G=32、mmap 和 prototype_expansion_budget 语义。",
        "区间为 (L,R]，中点不是精确最小阈值，也不保证达标。各目标停止宽度为 4。", "",
        "| N | 构图种子 | 目标 recall | 区间 (L,R] | recall(L) / recall(R) | 中点 | 中点 recall | 中点达标 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append(f"| {row['N']} | {row['seed']} | {row['target']:.2f} | ({row['L']},{row['R']}] | {row['recall_L']:.4f} / {row['recall_R']:.4f} | {row['estimate']} | {row['midpoint_recall']:.4f} | {'是' if row['midpoint_passes'] else '否'} |")
    lines += ["", "仅比较固定数据、查询下的区间位置；不据此推断普遍稳定性或统计显著性。",
              "新增种子未执行额外同预算对照；历史种子的已有测量保留，不重新扫描。"]
    (output / "report.md").write_text("\n".join(lines) + "\n")
    publish(output / "manifest.json", dict(sources=inventory, formal_point_counts=point_counts,
            analyzer_sha256=digest(__file__), reference=str(reference), seed43=str(seed43)))
    publish(output / "ANALYSIS_DONE.json", dict(exit_code=0, targets=len(rows),
            outputs={path.name: digest(path) for path in output.iterdir() if path.name != "ANALYSIS_DONE.json"}))
    print(output)
    for size in (20000, 50000):
        for target in (.95, .99):
            selected = [row for row in rows if row["N"] == size and row["target"] == target]
            print(size, target, [(row["seed"], row["L"], row["R"], row["estimate"]) for row in selected])
    print("PASS: 20 validated targets", point_counts)


if __name__ == "__main__":
    main()