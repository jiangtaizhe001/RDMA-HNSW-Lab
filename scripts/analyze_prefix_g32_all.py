#!/usr/bin/env python3
"""Validate ten completed scales and publish an auditable aggregate."""
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import platform

import numpy as np

from compute_gt import read_fvecs
from run_prefix_g32_efsearch import ROOT, SEMANTICS, digest, publish, validate

WIDTHS = {1000: 3, 2000: 3, 5000: 3, 10000: 4, 20000: 4, 50000: 4, 100000: 5,
          200000: 5, 500000: 5, 1000000: 6}
OUTPUT = ROOT / "out/prefix_g32_efsearch"


def load(path):
    return json.loads(path.read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def write_csv(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def check_process(record):
    path = Path(f"/proc/{record['pid']}/stat")
    if record.get("boot_id") == Path("/proc/sys/kernel/random/boot_id").read_text().strip() and path.exists():
        fields = path.read_text().rsplit(")", 1)[1].split()
        require(str(record.get("starttime")) != fields[19] or fields[0] == "Z", "experiment process still active")


def collect():
    sources = {}
    for campaign in sorted(OUTPUT.glob("campaign_*")):
        manifest_path = campaign / "manifest.json"
        if manifest_path.exists():
            identity = load(manifest_path).get("identity", {})
            if (identity.get("build_seed", 42) != 42 or
                    identity.get("experiment_kind") == "build_seed_comparison"):
                continue
        for size in WIDTHS:
            if (campaign / f"N{size}_DONE.json").exists():
                require(size not in sources, f"ambiguous campaigns for N={size}")
                sources[size] = campaign
    require(set(sources) == set(WIDTHS), "missing completed scale")
    curves, thresholds, pilots, inventory, provenance = [], [], [], {}, {}
    common_query = None
    largest_data = Path(load(sources[max(WIDTHS)] / "manifest.json")["data"])
    full_base = read_fvecs(largest_data / "base1000000.fvecs")
    require(full_base.shape == (1000000,128), "invalid 1M base shape")
    common_binaries = None
    for size, campaign in sorted(sources.items()):
        manifest = load(campaign / "manifest.json")
        identity = manifest["identity"]
        data = Path(manifest["data"])
        require(identity["protocol_version"] == "g32_threshold_midpoint_v2", "old protocol")
        require(identity["semantics"] == SEMANTICS and identity["queries"] == 1000, "semantics/query mismatch")
        require(all(identity[key] == value for key, value in dict(G=32,k=10,transport="mmap",cache_capacity=100000,
                    warmup=0,base_seed=42,query_seed=45,M=16,ef_construction=200).items()), "configuration mismatch")
        require(load(campaign / "status.json")["state"] == "completed", "batch incomplete")
        require(load(campaign / "exit.json")["exit_code"] == 0, "batch failed")
        done = load(campaign / f"N{size}_DONE.json")
        require(done["exit_code"] == 0 and done["scope"] == [size], "invalid scale completion")
        require(not (campaign / "SCAN_FAILED.json").exists(), "failed campaign")
        for process_path in [campaign / "controller.json", *campaign.glob("**/*process.json")]:
            check_process(load(process_path))
        if (campaign / "heartbeat.json").exists():
            check_process(load(campaign / "heartbeat.json")["monitor"])
        binaries = {name: sha for name, sha in identity["files"].items() if name.startswith("build/")}
        common_binaries = binaries if common_binaries is None else common_binaries
        require(binaries == common_binaries, "different experiment binaries")
        for name, sha in binaries.items():
            require(digest(ROOT / name) == sha, f"binary changed: {name}")
        for name, sha in manifest["inputs"].items():
            path = data / name
            require(digest(path) == sha, f"input changed: {path}")
            inventory[str(path)] = sha
        query_hash = digest(data / "queries.fvecs")
        common_query = query_hash if common_query is None else common_query
        require(query_hash == common_query, "query hashes differ")
        base = read_fvecs(data / f"base{size}.fvecs")
        queries = read_fvecs(data / "queries.fvecs")
        require(base.shape == (size,128) and queries.shape == (1000,128), "input shape mismatch")
        require(np.array_equal(base,full_base[:size]) and np.max(np.abs(base)) <= 1 and np.max(np.abs(queries)) <= 1,
                "base prefix/range mismatch")
        gt = np.fromfile(data / f"gt{size}.u32",dtype="<u4")
        require(gt.size == 10000 and np.all(gt < size), "invalid GT")
        gt = gt.reshape(1000,10)
        require(all(len(set(row)) == 10 for row in gt), "duplicate GT ID")
        base64 = base.astype(np.float64)
        for query_id in np.linspace(0,999,10,dtype=int):
            distances = np.sum((base64-queries[query_id].astype(np.float64))**2,axis=1)
            require(np.array_equal(np.argsort(distances,kind="stable")[:10],gt[query_id]), "independent GT mismatch")
        points = {}
        for phase, count in (("pilot",20),("runs",1000)):
            markers = sorted(campaign.glob(f"{phase}/n{size}/ef*/r0/complete.json"))
            if phase == "pilot":
                require({load(path)["key"]["ef"] for path in markers} == {min(value,size) for value in (64,256,1024,4096)}, "missing pilot")
            for marker in markers:
                complete = load(marker)
                key = complete["key"]
                ef = key["ef"]
                require(key["identity"] == identity and key["N"] == size and key["count"] == count and
                        key.get("repeat",0) == 0 and complete["exit_code"] == 0 and 10 <= ef <= size, "invalid point identity")
                expected_inputs = {name:manifest["inputs"][name] for name in
                    (f"index{size}.bin","pilot.fvecs" if phase == "pilot" else "queries.fvecs",
                     f"pilot_gt{size}.u32" if phase == "pilot" else f"gt{size}.u32")}
                require(key["inputs"] == expected_inputs, "point input mismatch")
                for name, sha in complete["outputs"].items():
                    path = marker.parent / name
                    require(digest(path) == sha, "point output changed")
                    inventory[str(path)] = sha
                row = validate(marker.parent/"summary.csv",marker.parent/"per_query.csv",size,ef,count)
                task = load(marker.parent/"task.json")
                record = dict(row,N=size,ef=ef,ef_over_N=ef/size,repeat=0,search_semantics=SEMANTICS,
                              purposes=";".join(task["purposes"]),campaign=str(campaign),
                              elapsed_seconds=complete["elapsed_seconds"])
                inventory[str(marker)] = digest(marker)
                if phase == "pilot":
                    pilots.append(record)
                else:
                    require(ef not in points,"duplicate formal point")
                    points[ef] = row["hits"]
                    curves.append(record)
        ordered = sorted(points)
        require(all(points[left] <= points[right] for left,right in zip(ordered,ordered[1:])),"nonmonotonic recall")
        for target in (9500,9900):
            path = campaign/f"targets/n{size}/recall{target//100:03d}.json"
            result = load(path)
            left,right,middle = result["L"],result["R"],result["estimate"]
            require(result["state"] == "estimated" and result["stopping_width"] == WIDTHS[size],"invalid target state/width")
            require(left in points and right in points and middle in points,"missing target point")
            require(0 < right-left <= WIDTHS[size] and middle == (left+right)//2,"invalid bracket/midpoint")
            require(points[left] < target <= points[right],"unbracketed target")
            require(all(result[field] == points[ef]/10000 for field,ef in
                        (("recall_L",left),("recall_R",right),("recall",middle))),"target recall mismatch")
            require(result["midpoint_passes"] == (points[middle] >= target),"midpoint status mismatch")
            if "evidence" in done:
                require(done["evidence"][str(path.relative_to(campaign))] == digest(path),"target evidence changed")
            thresholds.append(dict(N=size,target=target/10000,L=left,R=right,recall_L=points[left]/10000,
                recall_R=points[right]/10000,width=right-left,stopping_width=WIDTHS[size],estimate=middle,
                midpoint_recall=points[middle]/10000,midpoint_passes=points[middle]>=target,
                ef_over_N=middle/size,state="estimated",search_semantics=SEMANTICS,campaign=str(campaign)))
            inventory[str(path)] = digest(path)
        for path in (campaign/"manifest.json",campaign/f"N{size}_DONE.json",campaign/"exit.json"):
            inventory[str(path)] = digest(path)
        provenance[str(size)] = dict(campaign=str(campaign),manifest=manifest,exit=load(campaign/"exit.json"),
            initial_prediction=load(campaign/"initial_prediction.json") if (campaign/"initial_prediction.json").exists() else None,
            notes="Historical script hashes retained, not compared with evolved working tree; missing historical heartbeat/ETA fields are not reconstructed.")
    return curves,thresholds,pilots,inventory,provenance


def main():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    curves,thresholds,pilots,inventory,provenance = collect()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    campaign = OUTPUT/f"campaign_{stamp}_{digest(__file__)[:12]}_all_analysis"
    analysis = campaign/"analysis"
    figures = analysis/"figures"
    figures.mkdir(parents=True)
    manifest = dict(protocol_version="g32_threshold_midpoint_v2",search_semantics=SEMANTICS,
        stopping_widths=WIDTHS,sources=provenance,source_inventory=inventory,analyzer_sha256=digest(__file__),
        python=platform.python_version(),numpy=np.__version__,matplotlib=matplotlib.__version__)
    publish(campaign/"manifest.json",manifest)
    publish(campaign/"SCAN_DONE.json",dict(exit_code=0,scope=list(WIDTHS),targets=len(thresholds),
            evidence_inventory_sha256=digest(campaign/"manifest.json"),note="Validated aggregate of immutable source batches; no experiments rerun."))
    write_csv(analysis/"curve.csv",curves)
    write_csv(analysis/"thresholds.csv",thresholds)
    write_csv(analysis/"pilot.csv",pilots)
    for normalized in (False,True):
        figure,axis = plt.subplots(figsize=(10,6))
        for size in WIDTHS:
            rows = sorted((row for row in curves if row["N"] == size),key=lambda row:row["ef"])
            axis.plot([row["ef_over_N"] if normalized else row["ef"] for row in rows],
                      [float(row["recall"]) for row in rows],".-",label=f"N={size:,}")
        for target in (.95,.99):
            axis.axhline(target,color="gray",linestyle="--",linewidth=.8)
        axis.set(xlabel="efsearch / N (expansion budget)" if normalized else "efsearch (expansion budget)",
                 ylabel="recall@10",title=SEMANTICS,ylim=(0,1.01))
        axis.legend();axis.grid(alpha=.2);figure.tight_layout()
        figure.savefig(figures/("recall_ef_over_N.png" if normalized else "recall_ef.png"),dpi=160)
        plt.close(figure)
    lines = ["# G=32 十规模 efsearch-recall 统一实验报告","",f"汇总 campaign：`{campaign.relative_to(ROOT)}`。",
        "十规模二十个目标均通过原始数据核验；本报告未重新运行查询或把 pilot 混入正式数据。","",
        "## 实验协议","", "搜索语义：`search_semantics=prototype_expansion_budget`。efsearch 是扩展节点预算，不是标准 HNSW 候选束宽，不能直接与 hnswlib ef 对比。",
        "十规模独立构图；合成均匀 128 维 L2，base seed=42，M=16，ef_construction=200，构图 seed=42。共用同序 seed=45 的 1000 条查询；k=10，G=32，mmap，cache_capacity=100000，warmup=0，每点独立空缓存、运行一次。",
        "停止宽度依次为 3/3/3/4/4/4/5/5/5/6。首次停止后保留原区间并实测向下取整中点，不逐整数精扫。R 为实测达标参考，中点不保证达标，也不是精确最小阈值。","",
        "## 阈值结果","", "| N | 目标 recall | L / R | recall(L) / recall(R) | 区间宽度 | 中点估计 | 中点实测 recall / 是否达标 | 状态 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in thresholds:
        lines.append(f"| {row['N']} | {row['target']:.2f} | {row['L']} / {row['R']} | {row['recall_L']:.4f} / {row['recall_R']:.4f} | {row['width']} | {row['estimate']} | {row['midpoint_recall']:.4f} / {'是' if row['midpoint_passes'] else '否'} | estimated |")
    lines += ["","## 趋势与成本","", "各规模的中点预算及 ef/N 见下表。归一化预算不保证随规模单调变化，不能按 N 线性外推阈值。这些是固定图、固定查询集的观测；中点未达标时仍保留原估计，实测达标参考为 R。", "",
        "| N | 正式点数 | 0.95 中点 / N | 0.99 中点 / N | 正式客户端总耗时（秒） |", "| --- | --- | --- | --- | --- |"]
    for size in WIDTHS:
        rows = [row for row in curves if row["N"] == size]
        pair = [row for row in thresholds if row["N"] == size]
        lines.append(f"| {size} | {len(rows)} | {pair[0]['ef_over_N']:.6f} | {pair[1]['ef_over_N']:.6f} | {sum(row['elapsed_seconds'] for row in rows):.3f} |")
    lines += ["",f"共 {len(curves)} 个正式点、{len(pilots)} 个 pilot。成本原始字段（时延、QPS、node_reads、read_ops、字节及失败数）完整保留于 curve.csv；客户端耗时不含构图和 GT。mmap 性能不能解释为 RDMA 性能。", "",
        "![原始 ef 曲线](figures/recall_ef.png)","", "![归一化 ef 曲线](figures/recall_ef_over_N.png)","", "## 真值与验证","",
        "旧 GT 读取曾将 float32 位模式对应整数作数值转换；本轮按小端 float32 解码。十规模输入哈希、base 的 1M 前缀一致性、查询哈希、GT 行数/ID范围/无重复和固定 10 条查询直接差平方和 top-10 均通过。历史低 recall 与旧 GT 结果不可直接比较。",
        "逐点验证 summary 单行、正式逐查询 1000 行、pilot 20 行、无失败或更新，以及 prefix_reads=layer_reads、read_ops=prefix_reads+fallbacks、fallbacks=0、requested_bytes=useful_bytes+wasted_bytes。所有已测正式点 recall 非降，二十个目标的端点和中点均有有效完成标记及输出哈希。",
        "各批次控制进程和已记录子进程均已退出，退出码为 0。早期小规模批次缺少后续加入的心跳、完整停止映射或预测字段；本次按当前宽度独立核验数值，不声称历史调度接口完全满足现行协议。历史脚本哈希保留作来源记录，不要求等于当前演进版本；实验二进制一致并核验当前哈希。", "",
        "## 来源与估时","",f"Python={platform.python_version()}，NumPy={np.__version__}，Matplotlib={matplotlib.__version__}。输入、原始 CSV、完成证据 SHA-256 及源 manifest 全文见汇总 manifest.json；原始数据不复制、不改写。", "",
        "| N | 来源 campaign | 实际结束时间 | 初始预测 |", "| --- | --- | --- | --- |"]
    for size,source in provenance.items():
        prediction = source['initial_prediction']
        lines.append(f"| {size} | {Path(source['campaign']).name} | {source['exit'].get('finished_at','未记录')} | {json.dumps(prediction,ensure_ascii=False) if prediction else '未记录，不补造'} |")
    lines += ["", "部分历史预测仅为宽松预算上界，未准确扣除共享/已完成点，也未包含最终统一分析，不能当作全实验精确 ETA。原始预测和实际结束时间均保留，便于审计。", "",
        "## 限制","", "结果仅适用于指定图和固定 1000 查询，不保证跨种子、真实数据或未来分布。逐查询 CSV 不含结果 ID 或逐查询 recall，不能给出逐查询置信区间。曲线只连接必要实测点，不表示完成全预算扫描。未修改 C++、搜索语义或构建配置。"]
    report = "\n".join(lines)+"\n"
    (analysis/"report.md").write_text(report)
    docs = ROOT/"docs/prefix-g32-efsearch-report.md"
    prefix = "../"+str(figures.relative_to(ROOT))+"/"
    docs.write_text(report.replace("](figures/", "]("+prefix))
    outputs = {str(path.relative_to(campaign)):digest(path) for path in analysis.rglob("*") if path.is_file()}
    publish(campaign/"ANALYSIS_DONE.json",dict(exit_code=0,finished_at=datetime.now(timezone.utc).isoformat(),
        manifest_sha256=digest(campaign/"manifest.json"),outputs=outputs,docs_report_sha256=digest(docs)))
    publish(campaign/"status.json",dict(state="completed",scope=list(WIDTHS),targets=len(thresholds),formal_points=len(curves),pilot_points=len(pilots)))
    publish(campaign/"exit.json",dict(exit_code=0,finished_at=datetime.now(timezone.utc).isoformat()))
    print(campaign)
    print(f"PASS: {len(curves)} formal points, {len(pilots)} pilots, {len(thresholds)} targets")


if __name__ == "__main__":
    main()