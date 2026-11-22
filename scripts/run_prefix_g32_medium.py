#!/usr/bin/env python3
"""Execute selected G=32 batches without publishing global completion."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import struct
import subprocess
import sys
import time

import numpy as np

from compute_gt import exact_gt, read_fvecs
from run_prefix_g32_efsearch import COARSE, ROOT, digest, publish, validate, write_fvecs
from run_prefix_g32_10k import Batch, OUTPUT, SOURCE, heartbeat, now, process_identity

SIZES = (20000, 50000)
WIDTHS = {1000: 3, 2000: 3, 5000: 3, 10000: 4, 20000: 4, 50000: 4, 100000: 5,
          200000: 5, 500000: 5}
WIDTHS[1000000] = 6
COARSE = (*COARSE, 131072, 262144, 524288)


class MediumBatch(Batch):
    def __init__(self, campaign, sizes=SIZES, build_seed=42, reference=None):
        super().__init__(campaign)
        self.sizes = tuple(sizes)
        self.build_seed = build_seed
        self.reference = reference
        self.reference_manifest = None
        if reference is not None:
            marker = json.loads((reference / "ANALYSIS_DONE.json").read_text())
            if marker["exit_code"] != 0 or digest(reference / "manifest.json") != marker["manifest_sha256"]:
                raise ValueError("invalid reference analysis")
            self.reference_manifest = json.loads((reference / "manifest.json").read_text())
        self.source = ROOT / "data/prefix_g32_efsearch_v2" / campaign.name
        self.status.update(scope=list(self.sizes), targets={f"n{size}/recall{target}": {"state": "pending"}
                           for size in WIDTHS for target in ("095", "099")})
        self.all_points = {size: {} for size in self.sizes}
        self.rates = {}
        self.finished_sizes = set()
        self.frozen = {}

    def update(self, **values):
        super().update(**values)
        self.status["completed_points"] = sum(len(points) for points in self.all_points.values())
        publish(self.campaign / "status.json", self.status)

    def prepare(self):
        self.source.mkdir(parents=True, exist_ok=True)
        original = json.loads((SOURCE / "prepared.json").read_text())
        for name, sha in original["hashes"].items():
            if digest(SOURCE / name) != sha:
                raise ValueError(f"source hash mismatch: {name}")
        for name, sha in original["identity"]["files"].items():
            if name.startswith("build/") and digest(ROOT / name) != sha:
                raise ValueError(f"source binary mismatch: {name}")
        base = read_fvecs(SOURCE / "base100000.fvecs")
        queries = read_fvecs(SOURCE / "queries.fvecs")
        if base.shape != (100000, 128) or queries.shape != (1000, 128):
            raise ValueError("source dimensions/count mismatch")
        for name, matrix in (("base100000.fvecs", base), ("queries.fvecs", queries)):
            if np.max(np.abs(matrix)) > 1:
                raise ValueError("source values outside [-1,1]")
            with (SOURCE / name).open("rb") as stream:
                for row in matrix[:3]:
                    unpacked = struct.unpack("<i128f", stream.read(516))
                    if unpacked[0] != 128 or not np.array_equal(row, unpacked[1:]):
                        raise ValueError("independent float32 decode mismatch")
        reference = original["identity"]["reference_n1000"]
        reference_path = Path(reference["campaign"]) / "manifest.json"
        if digest(reference_path) != reference["manifest_sha256"]:
            raise ValueError("reference provenance mismatch")
        reference_data = Path(json.loads(reference_path.read_text())["data"])
        if (digest(SOURCE / "queries.fvecs") != reference["inputs"]["queries.fvecs"] or
                not np.array_equal(base[:1000], read_fvecs(reference_data / "base1000.fvecs"))):
            raise ValueError("query/base reference mismatch")
        files = ("scripts/run_prefix_g32_medium.py", "scripts/run_prefix_g32_10k.py",
                 "scripts/run_prefix_g32_efsearch.py", "scripts/compute_gt.py",
                 "build/dump_vectors", "build/hnsw_build", "build/baseline1_experiment")
        self.identity = dict(protocol_version="g32_threshold_midpoint_v2", search_semantics="prototype_expansion_budget",
                semantics="prototype_expansion_budget", scope=list(self.sizes), stopping_widths={str(key): value for key, value in WIDTHS.items()},
                G=32, k=10, queries=1000, transport="mmap", cache_capacity=100000, warmup=0,
                base_seed=42, query_seed=45, build_seed=self.build_seed, M=16, ef_construction=200,
                experiment_kind="build_seed_comparison" if self.reference else "threshold_scan",
                reference_analysis=dict(path=str(self.reference), sha256=digest(self.reference / "manifest.json")) if self.reference else None,
                source_provenance=dict(path=str(SOURCE / "prepared.json"), sha256=digest(SOURCE / "prepared.json")),
                files={name: digest(ROOT / name) for name in files})
        manifest_path = self.campaign / "manifest.json"
        preparation_path = self.campaign / "preparation.json"
        if preparation_path.exists() and json.loads(preparation_path.read_text())["identity"] != self.identity:
            raise ValueError("preparation identity changed; cannot resume")
        publish(preparation_path, dict(identity=self.identity, started_at=now(), data=str(self.source),
            memory=Path("/proc/meminfo").read_text(), disk_free_bytes=shutil.disk_usage(self.campaign).free))
        if manifest_path.exists() and json.loads(manifest_path.read_text())["identity"] != self.identity:
            raise ValueError("campaign identity changed; cannot resume")
        for name in ("base100000.fvecs", "queries.fvecs", "pilot.fvecs"):
            destination = self.source / name
            if destination.exists():
                if digest(destination) != original["hashes"][name]:
                    raise ValueError(f"copied input mismatch: {name}")
            else:
                shutil.copyfile(SOURCE / name, destination)
        checks = {}
        if max(self.sizes) > len(base):
            generated = self.source / "base1000000.fvecs"
            generation_marker = self.source / "generated1000000.json"
            command = [str(ROOT / "build/dump_vectors"), "--count", "1000000", "--dim", "128",
                       "--seed", "42", "--output", str(generated)]
            self.update(current=dict(N=1000000, ef=None, purpose="generate_base"))
            if generation_marker.exists():
                saved_generation = json.loads(generation_marker.read_text())
                if saved_generation["identity"] != self.identity or digest(generated) != saved_generation["sha256"]:
                    raise ValueError("1M generation identity/hash mismatch")
            else:
                with (self.campaign / "generate1000000.log").open("a") as stream:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream)
                    publish(self.campaign / "generate1000000_process.json",
                            dict(process_identity(process.pid), command=command, started_at=now()))
                    try:
                        code = process.wait(timeout=3600)
                    except BaseException:
                        process.kill()
                        process.wait()
                        raise
                    if code:
                        raise RuntimeError(f"base generation failed: {code}")
                publish(generation_marker, dict(identity=self.identity, command=command, sha256=digest(generated)))
            generated_base = read_fvecs(generated)
            if generated_base.shape != (1000000, 128) or np.max(np.abs(generated_base)) > 1:
                raise ValueError("invalid 1M base shape/range")
            if not np.array_equal(generated_base[:len(base)], base):
                raise ValueError("1M base does not preserve verified 100K prefix")
            base = generated_base
        for size in self.sizes:
            self.update(current=dict(N=size, ef=None, purpose="prepare"))
            marker = self.source / f"prepared{size}.json"
            if marker.exists():
                saved = json.loads(marker.read_text())
                if saved["identity"] != self.identity:
                    raise ValueError("prepared identity mismatch")
                for name, sha in saved["hashes"].items():
                    if digest(self.source / name) != sha:
                        raise ValueError(f"prepared input changed: {name}")
            else:
                if size != 1000000:
                    write_fvecs(self.source / f"base{size}.fvecs", base[:size])
                command = [str(ROOT / "build/hnsw_build"), "--input", str(self.source / f"base{size}.fvecs"),
                           "--dim", "128", "--M", "16", "--ef-construction", "200", "--seed", str(self.build_seed),
                           "--output", str(self.source / f"index{size}.bin")]
                with (self.campaign / f"build{size}.log").open("a") as stream:
                    stream.write(json.dumps(command) + "\n")
                    stream.flush()
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream)
                    publish(self.campaign / f"build{size}_process.json", dict(process_identity(process.pid), command=command, started_at=now()))
                    try:
                        code = process.wait(timeout=86400 if size == 1000000 else 3600)
                    except BaseException:
                        process.kill()
                        process.wait()
                        raise
                    if code:
                        raise RuntimeError(f"index build failed: {size}, exit={code}")
                gt, checked = exact_gt(base[:size], queries)
                gt.tofile(self.source / f"gt{size}.u32")
                gt[:20].tofile(self.source / f"pilot_gt{size}.u32")
                names = (f"base{size}.fvecs", f"index{size}.bin", f"gt{size}.u32", f"pilot_gt{size}.u32")
                saved = dict(identity=self.identity, hashes={name: digest(self.source / name) for name in names},
                             gt_checks=checked, build_command=command)
                publish(marker, saved)
            if not np.array_equal(read_fvecs(self.source / f"base{size}.fvecs"), base[:size]):
                raise ValueError("base prefix mismatch")
            gt = np.fromfile(self.source / f"gt{size}.u32", dtype="<u4")
            if gt.size != 10000 or np.any(gt >= size):
                raise ValueError("invalid GT shape or ID range")
            gt = gt.reshape(1000, 10)
            if any(len(set(row)) != 10 for row in gt):
                raise ValueError("duplicate GT IDs")
            base64 = base[:size].astype(np.float64)
            for query_id in saved["gt_checks"]["checked_queries"]:
                distances = np.sum((base64 - queries[query_id].astype(np.float64)) ** 2, axis=1)
                if not np.array_equal(np.argsort(distances, kind="stable")[:10], gt[query_id]):
                    raise ValueError("independent GT check failed")
            if (self.source / f"gt{size}.u32").stat().st_size != 40000:
                raise ValueError("GT byte size mismatch")
            if (self.source / f"pilot_gt{size}.u32").read_bytes() != gt[:20].tobytes():
                raise ValueError("pilot GT mismatch")
            checks[str(size)] = saved["gt_checks"]
            print(f"{now()} prepared N={size}, independent GT checks passed", flush=True)
        if not np.array_equal(read_fvecs(self.source / "pilot.fvecs"), queries[:20]):
            raise ValueError("pilot query mismatch")
        names = ["base100000.fvecs", "queries.fvecs", "pilot.fvecs"] + [
                 f"{prefix}{size}.{extension}" for size in self.sizes
                 for prefix, extension in (("base", "fvecs"), ("index", "bin"), ("gt", "u32"), ("pilot_gt", "u32"))]
        self.inputs = {name: digest(self.source / name) for name in names}
        import matplotlib
        manifest = dict(identity=self.identity, data=str(self.source), inputs=self.inputs,
                        input_paths={name: str(self.source / name) for name in names}, gt_checks=checks,
                        python=platform.python_version(), numpy=np.__version__, matplotlib=matplotlib.__version__,
                        memory=Path("/proc/meminfo").read_text(), disk_free_bytes=shutil.disk_usage(self.campaign).free)
        if manifest_path.exists():
            if json.loads(manifest_path.read_text())["inputs"] != self.inputs:
                raise ValueError("resume inputs changed")
        else:
            publish(manifest_path, manifest)

    def estimate_remaining(self):
        if any(size not in self.rates for size in self.sizes):
            return dict(estimated_finish_at_range=None, reason="awaiting selected sizes' pilot calibration")
        work = 0
        for size in self.sizes:
            if size in self.finished_sizes:
                continue
            points = self.all_points[size]
            budgets = set()
            extra = 0
            if not points or max(points.values()) < 9900:
                budgets.update(({value for value in COARSE if value <= size} | {size}) - points.keys())
            for target in (9500, 9900):
                if (size, target) in self.frozen:
                    result = self.frozen[size, target]
                    if result.get("estimate") not in points and result.get("estimate") is not None:
                        budgets.add(result["estimate"])
                    continue
                passed = [ef for ef in sorted(points) if points[ef] >= target]
                if passed and passed[0] == 10:
                    continue
                right = passed[0] if passed else size
                left = max((ef for ef in points if ef < right and points[ef] < target), default=10)
                steps = max(0, math.ceil(math.log2(max(1, (right - left) / WIDTHS[size]))))
                extra += (steps + 1) * right
            work += self.rates[size] * 1000 * 1.5 * (sum(budgets) + extra)
        timestamp = datetime.now().astimezone()
        return dict(estimated_at=timestamp.isoformat(), estimated_remaining_seconds_range=[0, work],
                    estimated_finish_at_range=[timestamp.isoformat(), (timestamp + timedelta(seconds=work)).isoformat()],
                    timezone=timestamp.tzname(), coverage=f"N={self.sizes} remaining coarse, binary and midpoint points; no global analysis",
                    basis="per-size max observed seconds/query/ef * 1.5; unknown binary points bounded by current R; conservative upper bound")

    def measure(self, ef, purpose):
        if purpose != "pilot":
            self.status["batch_estimate"] = self.estimate_remaining()
        hits = super().measure(ef, purpose)
        self.rates[self.size] = self.rate
        self.status["batch_estimate"] = self.estimate_remaining()
        self.update()
        return hits

    def target(self, target):
        width = WIDTHS[self.size]
        path = self.campaign / f"targets/n{self.size}/recall{target // 100:03d}.json"
        def save(result):
            result = dict(result, N=self.size, target=target / 10000, stopping_width=width,
                          stopping_widths=WIDTHS, identity=self.identity)
            publish(path, result)
            self.status["targets"][f"n{self.size}/recall{target // 100:03d}"] = result
            self.update(current_target=target / 10000)
            return result
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous and previous["identity"] != self.identity:
            raise ValueError("target identity mismatch")
        if previous.get("state") in ("frozen", "estimated", "at_minimum", "unreached"):
            for ef in (previous.get("L"), previous.get("R")):
                if ef is not None:
                    self.measure(ef, "resume_evidence")
            result = previous
        elif self.points[10] >= target:
            result = dict(state="at_minimum", L=None, R=10, estimate=10,
                          recall=self.points[10] / 10000, midpoint_passes=True)
        elif max(self.points.values()) < target:
            if self.size not in self.points:
                raise ValueError("unreached without ef=N evidence")
            result = dict(state="unreached", L=None, R=None, estimate=None,
                          max_ef=self.size, max_recall=max(self.points.values()) / 10000)
        else:
            right = min(ef for ef in self.points if self.points[ef] >= target)
            left = max(ef for ef in self.points if ef < right and self.points[ef] < target)
            history = previous.get("history", [])
            while right - left > width:
                middle = (left + right) // 2
                save(dict(state="running", L=left, R=right, history=history, next_ef=middle))
                hits = self.measure(middle, "binary")
                history.append(dict(L=left, R=right, ef=middle, hits=hits))
                if hits >= target:
                    right = middle
                else:
                    left = middle
                save(dict(state="running", L=left, R=right, history=history,
                          next_ef=(left + right) // 2))
            result = dict(state="frozen", L=left, R=right, width=right - left,
                          recall_L=self.points[left] / 10000, recall_R=self.points[right] / 10000,
                          estimate=(left + right) // 2, history=history)
            save(result)
        self.frozen[self.size, target] = result
        if result["state"] in ("frozen", "estimated"):
            hits = self.measure(result["estimate"], "midpoint")
            result.update(state="estimated", recall=hits / 10000, midpoint_passes=hits >= target)
        save(result)

    def scan(self):
        self.prepare()
        for size in self.sizes:
            self.size, self.points, self.rate = size, self.all_points[size], 0
            for ef in (64, 256, 1024, 4096):
                self.measure(ef, "pilot")
        for size in self.sizes:
            self.size, self.points, self.rate = size, self.all_points[size], self.rates[size]
            for marker in sorted((self.campaign / f"runs/n{size}").glob("ef*/r0/complete.json")):
                ef = json.loads(marker.read_text())["key"]["ef"]
                self.measure(ef, "resume_evidence")
            for ef in sorted({value for value in COARSE if value <= size} | {size}):
                if 10 in self.points and max(self.points.values()) >= 9900:
                    break
                self.measure(ef, "coarse")
            for target in (9500, 9900):
                self.target(target)
            if self.reference:
                self.compare_reference_points()
            for name, sha in self.inputs.items():
                if digest(self.source / name) != sha:
                    raise ValueError("final input hash mismatch")
            evidence = {str(path.relative_to(self.campaign)): digest(path)
                        for path in (self.campaign / f"targets/n{size}").glob("*.json")}
            publish(self.campaign / f"N{size}_DONE.json", dict(exit_code=0, scope=[size],
                    full_experiment_complete=False, finished_at=now(), evidence=evidence))
            self.finished_sizes.add(size)
            self.update(batch_estimate=self.estimate_remaining())
        if self.reference:
            self.write_comparison()
        self.update(state="completed", current=None, current_target=None, estimated_remaining_seconds=0,
                    estimated_finish_at=now(), batch_estimate=self.estimate_remaining())


    def reference_campaign(self, size):
        source = self.reference_manifest["sources"][str(size)]
        campaign = Path(source["campaign"])
        manifest = json.loads((campaign / "manifest.json").read_text())
        if manifest != source["manifest"] or manifest["identity"].get("build_seed", 42) != 42:
            raise ValueError("reference manifest changed or seed is not 42")
        for name in (f"base{size}.fvecs", "queries.fvecs", f"gt{size}.u32"):
            if manifest["inputs"][name] != self.inputs[name]:
                raise ValueError(f"comparison input mismatch: {name}")
        return campaign

    def reference_target(self, size, target):
        campaign = self.reference_campaign(size)
        path = campaign / f"targets/n{size}/recall{target}.json"
        expected = self.reference_manifest["source_inventory"][str(path)]
        if digest(path) != expected:
            raise ValueError("reference target changed")
        return json.loads(path.read_text())

    def compare_reference_points(self):
        budgets = set()
        for target in ("095", "099"):
            result = self.reference_target(self.size, target)
            budgets.update((result["L"], result["R"], result["estimate"]))
        for ef in sorted(budgets):
            self.measure(ef, "reference_seed42")

    def write_comparison(self):
        directory = self.campaign / "comparison"
        directory.mkdir(exist_ok=True)
        thresholds, points = [], []
        for size in self.sizes:
            reference = self.reference_campaign(size)
            for target in ("095", "099"):
                old = self.reference_target(size, target)
                new = json.loads((self.campaign / f"targets/n{size}/recall{target}.json").read_text())
                if new["state"] != "estimated":
                    raise ValueError("comparison requires estimated target evidence")
                thresholds.append(dict(N=size, target=int(target) / 100, reference_seed=42,
                    build_seed=self.build_seed, reference_L=old["L"], reference_R=old["R"],
                    reference_estimate=old["estimate"], L=new["L"], R=new["R"], estimate=new["estimate"],
                    estimate_delta=new["estimate"] - old["estimate"],
                    estimate_delta_percent=100 * (new["estimate"] / old["estimate"] - 1),
                    reference_midpoint_recall=old["recall"], midpoint_recall=new["recall"],
                    midpoint_passes=new["midpoint_passes"]))
            for ef in sorted({value for target in ("095", "099")
                              for key, value in self.reference_target(size, target).items()
                              if key in ("L", "R", "estimate")}):
                path = reference / f"runs/n{size}/ef{ef}/r0"
                complete = json.loads((path / "complete.json").read_text())
                for name, sha in complete["outputs"].items():
                    if digest(path / name) != sha:
                        raise ValueError("reference point output changed")
                old = validate(path / "summary.csv", path / "per_query.csv", size, ef, 1000)
                new_recall = self.all_points[size][ef] / 10000
                points.append(dict(N=size, ef=ef, reference_seed=42, build_seed=self.build_seed,
                    reference_recall=float(old["recall"]), recall=new_recall,
                    recall_delta=new_recall - float(old["recall"])))
        for name, rows in (("thresholds.csv", thresholds), ("matched_budgets.csv", points)):
            with (directory / name).open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        lines = ["# Build Seed Comparison", "", f"Construction seeds: 42 vs {self.build_seed}.",
            "Base seed=42, query seed=45; identical base/query/GT hashes. G=32, mmap, k=10, 1000 queries.",
            "Search semantics: prototype_expansion_budget. Midpoints estimate brackets, not exact minimum passing budgets.",
            "", "| N | Target | Seed 42 bracket / midpoint | New bracket / midpoint | Delta | Delta % | New midpoint recall |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
        for row in thresholds:
            lines.append(f"| {row['N']} | {row['target']:.2f} | ({row['reference_L']},{row['reference_R']}] / {row['reference_estimate']} | ({row['L']},{row['R']}] / {row['estimate']} | {row['estimate_delta']:+d} | {row['estimate_delta_percent']:+.3f}% | {row['midpoint_recall']:.4f} |")
        lines += ["", "## Matched Expansion Budgets", "", "| N | ef | Seed 42 recall | New recall | Difference (percentage points) |",
                  "| --- | --- | --- | --- | --- |"]
        for row in points:
            lines.append(f"| {row['N']} | {row['ef']} | {row['reference_recall']:.4f} | {row['recall']:.4f} | {100 * row['recall_delta']:+.2f} |")
        lines += ["", "Only two construction seeds are compared; this does not estimate variance across seeds.",
                  "Latency is not a controlled seed effect and mmap timings are not RDMA performance."]
        (directory / "report.md").write_text("\n".join(lines) + "\n")
        publish(self.campaign / "COMPARISON_DONE.json", dict(exit_code=0, finished_at=now(),
            reference=str(self.reference), reference_manifest_sha256=digest(self.reference / "manifest.json"),
            outputs={str(path.relative_to(self.campaign)): digest(path) for path in directory.iterdir()}))


def control(campaign, sizes=SIZES, build_seed=42, reference=None):
    with (campaign / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        batch = MediumBatch(campaign, sizes, build_seed, reference)
        batch.update()
        publish(campaign / "controller.json", dict(process_identity(os.getpid()), started_at=now(),
                command=[sys.executable, *sys.argv]))
        monitor = subprocess.Popen([sys.executable, __file__, "--heartbeat", str(campaign), "--parent", str(os.getpid())],
                                   stdin=subprocess.DEVNULL)
        def interrupt(signum, frame):
            raise InterruptedError(f"signal {signum}")
        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        code, reason = 0, f"N={sizes} batches completed; global completion requires independent verification"
        try:
            batch.scan()
            (campaign / "SCAN_FAILED.json").unlink(missing_ok=True)
        except BaseException as error:
            code, reason = 1, repr(error)
            batch.update(state="interrupted" if isinstance(error, InterruptedError) else "failed", error=reason)
            publish(campaign / "SCAN_FAILED.json", dict(exit_code=code, reason=reason, finished_at=now()))
            raise
        finally:
            monitor.terminate()
            monitor.wait()
            publish(campaign / "exit.json", dict(exit_code=code, reason=reason, finished_at=now()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--heartbeat", type=Path)
    parser.add_argument("--parent", type=int)
    parser.add_argument("--build-seed", type=int, default=42)
    parser.add_argument("--compare-reference", type=Path)
    parser.add_argument("--sizes", type=int, nargs="+", choices=(20000, 50000, 100000, 200000, 500000, 1000000), default=list(SIZES))
    args = parser.parse_args()
    if not 0 <= args.build_seed <= 4294967295:
        parser.error("build seed must fit uint32")
    reference = args.compare_reference.resolve() if args.compare_reference else None
    sizes = tuple(sorted(set(args.sizes)))
    if args.heartbeat:
        heartbeat(args.heartbeat, args.parent)
    elif args.launch:
        campaign = (args.campaign or OUTPUT / ("campaign_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") +
                    "_" + digest(__file__)[:12] + f"_seed{args.build_seed}_" + "_".join(f"n{size}" for size in sizes))).resolve()
        if campaign.parent != OUTPUT:
            raise ValueError("campaign must reside under repository output root")
        campaign.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(campaign).free < 2 * 1024 ** 3:
            raise RuntimeError("less than 2 GiB free disk space")
        with (campaign / "controller.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (campaign / "controller.log").open("a") as stream:
                process = subprocess.Popen(["nohup", sys.executable, __file__, "--campaign", str(campaign),
                    "--sizes", *map(str, sizes), "--build-seed", str(args.build_seed),
                    *(["--compare-reference", str(reference)] if reference else [])],
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True)
        for attempt in range(100):
            marker = campaign / "controller.json"
            if marker.exists() and json.loads(marker.read_text())["pid"] == process.pid:
                print(json.dumps(dict(campaign=str(campaign), pid=process.pid, current=f"preparing N={sizes}",
                    message="Background started; point and batch ETA pending pilot calibration; ten-size final verification and analysis ETA not calibrated.",
                    timezone=datetime.now().astimezone().tzname(), status=str(campaign / "status.json")), indent=2))
                return
            if process.poll() is not None:
                raise RuntimeError(f"controller startup failed: {campaign}")
            time.sleep(0.05)
        raise RuntimeError("startup not confirmed")
    elif args.campaign:
        control(args.campaign.resolve(), sizes, args.build_seed, reference)
    else:
        parser.error("use --launch or --campaign")


if __name__ == "__main__":
    main()