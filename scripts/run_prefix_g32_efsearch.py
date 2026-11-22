#!/usr/bin/env python3
"""Reproducible G=32 prototype expansion-budget experiment (mmap only)."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
import fcntl

import numpy as np
from compute_gt import exact_gt, read_fvecs

ROOT = Path(__file__).resolve().parents[1]
SIZES = (10000, 20000, 50000, 100000)
STOP_WIDTH = {1000: 3, 2000: 3, 5000: 3, 10000: 4, 100000: 5}
SEMANTICS = "prototype_expansion_budget"
COARSE = (10, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096,
          8192, 16384, 32768, 65536)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def publish(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def write_fvecs(path, vectors):
    records = np.empty((len(vectors), vectors.shape[1] + 1), dtype="<i4")
    records[:, 0] = vectors.shape[1]
    records[:, 1:] = vectors.astype("<f4").view("<i4")
    records.tofile(path)


def read_csv(path):
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(f"duplicate/missing CSV columns: {path}")
        rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError(f"malformed CSV: {path}")
    return rows


def validate(summary, per_query, size, ef, count):
    rows = read_csv(summary)
    samples = read_csv(per_query)
    if len(rows) != 1 or len(samples) != count:
        raise ValueError("summary/per-query row count mismatch")
    row = rows[0]
    expected = dict(strategy="quantized_prefix", transport="mmap", dim="128",
                    node_count_start=str(size), node_count_end=str(size), M="16",
                    ef_search=str(ef), k="10", queries=str(count), updates="0",
                    cache_capacity="100000", prefix_granularity="32",
                    ef_construction="200", dataset="synthetic")
    for field, value in expected.items():
        if row[field] != value:
            raise ValueError(f"{field}: {row[field]} != {value}")
    for record in [row, *samples]:
        number = lambda field: int(record[field])
        for field in ("failed_reads", "fallbacks", "prefix_failures", "tail_failures",
                      "malformed_degrees", "transport_write_ops"):
            if number(field):
                raise ValueError(f"unexpected {field}")
        if (number("prefix_reads") != number("layer_reads") or
                number("read_ops") != number("prefix_reads") + number("fallbacks") or
                number("requested_bytes") != number("useful_bytes") + number("wasted_bytes")):
            raise ValueError("prefix accounting invariant failed")
    if int(row["update_failures"]) or [int(item["query_idx"]) for item in samples] != list(range(count)):
        raise ValueError("updates failed or query sequence mismatch")
    for field in ("requested_bytes", "useful_bytes", "wasted_bytes", "prefix_reads", "read_ops"):
        if int(row[field]) != sum(int(item[field]) for item in samples):
            raise ValueError(f"summary/per-query {field} mismatch")
    recall = float(row["recall"])
    hits = round(recall * count * 10)
    if not np.isfinite(recall) or not 0 <= hits <= count * 10 or abs(recall - hits / (count * 10)) > 5e-7:
        raise ValueError("invalid recall precision")
    return dict(row, hits=hits)


class Experiment:
    def __init__(self, args):
        self.args = args
        self.out = args.out.resolve()
        self.data = args.data.resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.identity = {"semantics": SEMANTICS, "G": 32, "k": 10,
                         "transport": "mmap", "cache_capacity": 100000, "warmup": 0,
                         "base_seed": 42, "query_seed": 45, "M": 16,
                         "ef_construction": 200, "queries": 1000,
                         "files": {str(path.relative_to(ROOT)): digest(path) for path in
                                   [ROOT / "build" / name for name in
                                    ("dump_vectors", "hnsw_build", "baseline1_experiment")] +
                                   [ROOT / "scripts" / name for name in
                                    ("compute_gt.py", "run_prefix_g32_efsearch.py",
                                     "analyze_prefix_g32_efsearch.py")]}}
        self.inputs = {}

    def command(self, command, log):
        with log.open("w") as stream:
            stream.write(json.dumps([str(item) for item in command]) + "\n")
            stream.flush()
            subprocess.run([str(item) for item in command], stdout=stream,
                           stderr=subprocess.STDOUT, check=True, timeout=self.args.timeout)

    def prepare(self):
        marker = self.data / "prepared.json"
        if marker.exists():
            saved = json.loads(marker.read_text())
            if saved["identity"] != self.identity:
                raise ValueError("input provenance changed; use new --data and --out directories")
            for path, sha in saved["hashes"].items():
                if digest(self.data / path) != sha:
                    raise ValueError(f"input hash changed: {path}")
            self.inputs = saved["hashes"]
            return
        if any(self.data.iterdir()):
            raise ValueError("unverified partial inputs; use a fresh --data directory")
        self.command([ROOT / "build/dump_vectors", "--count", 100000, "--dim", 128,
                      "--seed", 42, "--output", self.data / "base100000.fvecs"],
                     self.out / "generate_base.log")
        self.command([ROOT / "build/dump_vectors", "--count", 1000, "--dim", 128,
                      "--seed", 45, "--output", self.data / "queries.fvecs"],
                     self.out / "generate_queries.log")
        base = read_fvecs(self.data / "base100000.fvecs")
        queries = read_fvecs(self.data / "queries.fvecs")
        if base.shape != (100000, 128) or queries.shape != (1000, 128):
            raise ValueError("generator shape mismatch")
        for path, matrix in [(self.data / "base100000.fvecs", base),
                             (self.data / "queries.fvecs", queries)]:
            if np.max(np.abs(matrix)) > 1:
                raise ValueError("synthetic vector outside [-1,1]")
            with path.open("rb") as stream:
                unpacked = struct.unpack("<i128f", stream.read(516))
            if not np.array_equal(matrix[0], unpacked[1:]):
                raise ValueError("independent float32 decode mismatch")
        write_fvecs(self.data / "pilot.fvecs", queries[:20])
        checks = {}
        for size in SIZES:
            base_path = self.data / f"base{size}.fvecs"
            if size != 100000:
                write_fvecs(base_path, base[:size])
            index = self.data / f"index{size}.bin"
            self.command([ROOT / "build/hnsw_build", "--input", base_path, "--dim", 128,
                          "--M", 16, "--ef-construction", 200, "--seed", 42,
                          "--output", index], self.out / f"build{size}.log")
            gt, checks[str(size)] = exact_gt(base[:size], queries)
            gt.tofile(self.data / f"gt{size}.u32")
            gt[:20].tofile(self.data / f"pilot_gt{size}.u32")
            print(f"prepared N={size}: GT independently checked", flush=True)
        self.inputs = {path.name: digest(path) for path in self.data.iterdir() if path.is_file()}
        publish(marker, dict(identity=self.identity, hashes=self.inputs, gt_checks=checks))

    def run(self, size, ef, repeat=0, phase="coarse"):
        if time.monotonic() - self.started > self.args.budget_seconds:
            raise TimeoutError("session execution budget exhausted; resume with the same command")
        pilot = phase == "pilot"
        count = 20 if pilot else 1000
        query = self.data / ("pilot.fvecs" if pilot else "queries.fvecs")
        gt = self.data / f"{'pilot_gt' if pilot else 'gt'}{size}.u32"
        index = self.data / f"index{size}.bin"
        key = dict(identity=self.identity, N=size, ef=ef, repeat=repeat, count=count,
                   inputs={path.name: self.inputs[path.name] for path in (index, query, gt)})
        directory = self.out / ("pilot" if pilot else f"n{size}")
        label = f"n{size}_g32_ef{ef}_r{repeat}"
        for folder in ("summary", "per_query", "logs", "completed"):
            (directory / folder).mkdir(parents=True, exist_ok=True)
        summary = directory / "summary" / f"{label}.csv"
        per_query = directory / "per_query" / f"{label}.csv"
        marker = directory / "completed" / f"{label}.json"
        if marker.exists():
            saved = json.loads(marker.read_text())
            if saved["key"] != key or any(digest(directory / name) != sha for name, sha in saved["outputs"].items()):
                raise ValueError(f"stale/corrupt completed run: {marker}")
            return validate(summary, per_query, size, ef, count)
        if digest(index) != self.inputs[index.name]:
            raise ValueError("index changed before run")
        print(f"{phase}: N={size} ef={ef} repeat={repeat}", flush=True)
        started = time.monotonic()
        with tempfile.TemporaryDirectory(dir=directory, prefix="pending_") as temporary:
            pending = Path(temporary)
            command = [ROOT / "build/baseline1_experiment", "--transport", "mmap",
                       "--index", index, "--queries", query, "--gt", gt,
                       "--read-strategy", "prefix", "--prefix-granularity", 32,
                       "--ef-search", ef, "--k", 10, "--cache-capacity", 100000,
                       "--warmup", 0, "--ef-construction", 200, "--seed", 42,
                       "--dataset", "synthetic", "--workload", "query", "--label", label,
                       "--out", pending / "per_query.csv", "--summary-out", pending / "summary.csv"]
            self.command(command, directory / "logs" / f"{label}.log")
            row = validate(pending / "summary.csv", pending / "per_query.csv", size, ef, count)
            if digest(index) != self.inputs[index.name]:
                raise ValueError("index changed during run")
            (pending / "summary.csv").replace(summary)
            (pending / "per_query.csv").replace(per_query)
        outputs = {str(path.relative_to(directory)): digest(path) for path in (summary, per_query)}
        publish(marker, dict(key=key, phase=phase, outputs=outputs,
                             elapsed_seconds=time.monotonic() - started, exit_code=0))
        return row

    def scan(self):
        for size in SIZES:
            for ef in (64, 256, 1024, min(4096, size)):
                self.run(size, ef, phase="pilot")
        estimates = {}
        for size in SIZES:
            markers = list((self.out / "pilot/completed").glob(f"n{size}_*.json"))
            times = [json.loads(path.read_text()) for path in markers]
            rate = max(item["elapsed_seconds"] / item["key"]["ef"] / 20 for item in times)
            estimates[str(size)] = dict(full_coarse_seconds=rate * sum(sorted({ef for ef in COARSE if ef <= size} | {size})) * 1000,
                                        ef_N_seconds=rate * size * 1000)
        publish(self.out / "pilot/estimates.json", estimates)
        print(f"pilot estimates: {json.dumps(estimates)}", flush=True)
        if self.args.phase == "pilot":
            return
        if sum(item["full_coarse_seconds"] for item in estimates.values()) > self.args.max_estimated_seconds:
            raise TimeoutError("pilot estimated coarse cost exceeds --max-estimated-seconds; approval needed to increase budget")
        for size in SIZES:
            points = {}
            def measure(ef, repeat=0, phase="fine"):
                row = self.run(size, ef, repeat, phase)
                if repeat == 0:
                    points[ef] = int(row["hits"])
                    ordered = sorted(points)
                    if any(points[left] > points[right] for left, right in zip(ordered, ordered[1:])):
                        raise ValueError("nonmonotonic recall; binary refinement stopped")
                elif int(row["hits"]) != points[ef]:
                    raise ValueError("repeat recall conflict")
                return int(row["hits"])
            for ef in sorted({value for value in COARSE if value <= size} | {size}):
                measure(ef, phase="coarse")
            for target in (9500, 9900):
                passed = [ef for ef in sorted(points) if points[ef] >= target]
                if not passed:
                    continue
                upper = passed[0]
                lower = max([ef for ef in points if ef < upper and points[ef] < target], default=9)
                while upper - lower > 16:
                    middle = (lower + upper) // 2
                    if measure(middle) >= target:
                        upper = middle
                    else:
                        lower = middle
                for ef in range(max(10, lower), upper + 1):
                    if ef not in points:
                        measure(ef)
                threshold = min(ef for ef in points if points[ef] >= target)
                for ef in ([threshold - 1, threshold] if threshold > 10 else [10]):
                    for repeat in (1, 2):
                        measure(ef, repeat, "repeat")


def locate_target(measure, points, target, width, checkpoint):
    if points[10] >= target:
        return dict(state="at_minimum", estimate=10, recall=points[10] / 10000,
                    midpoint_passes=True, L=None, R=10)
    passed = [ef for ef in sorted(points) if points[ef] >= target]
    if not passed:
        return dict(state="unreached", estimate=None, L=None, R=None,
                    max_ef=max(points), max_recall=max(points.values()) / 10000)
    right = passed[0]
    left = max(ef for ef in points if ef < right and points[ef] < target)
    history = []
    while right - left > width:
        middle = (left + right) // 2
        hits = measure(middle, "binary")
        history.append(dict(L=left, R=right, ef=middle, hits=hits))
        if hits >= target:
            right = middle
        else:
            left = middle
        checkpoint(dict(state="running", L=left, R=right, history=history))
    estimate = (left + right) // 2
    hits = measure(estimate, "midpoint")
    return dict(state="estimated", L=left, R=right, width=right - left,
                recall_L=points[left] / 10000, recall_R=points[right] / 10000,
                estimate=estimate, recall=hits / 10000,
                midpoint_passes=hits >= target, history=history)


class SmallExperiment(Experiment):
    def run(self, size, ef, repeat=0, phase="coarse"):
        pilot = phase == "pilot"
        count = 20 if pilot else 1000
        directory = self.out / ("pilot" if pilot else "runs") / f"n{size}" / f"ef{ef}" / "r0"
        directory.mkdir(parents=True, exist_ok=True)
        query = self.data / ("pilot.fvecs" if pilot else "queries.fvecs")
        gt = self.data / f"{'pilot_gt' if pilot else 'gt'}{size}.u32"
        index = self.data / f"index{size}.bin"
        key = dict(identity=self.identity, N=size, ef=ef, count=count,
                   inputs={path.name: self.inputs[path.name] for path in (index, query, gt)})
        marker = directory / "complete.json"
        summary, per_query = directory / "summary.csv", directory / "per_query.csv"
        if marker.exists():
            saved = json.loads(marker.read_text())
            if saved["key"] != key or saved["outputs"] != {path.name: digest(path) for path in (summary, per_query)}:
                raise ValueError("stale completed point")
            row = validate(summary, per_query, size, ef, count)
            task = json.loads((directory / "task.json").read_text())
            task["purposes"] = sorted(set(task["purposes"] + [phase]))
            publish(directory / "task.json", task)
            return row
        predicted = getattr(self, "rate", 0) * ef * count * 1.5
        if predicted > 1800:
            raise RuntimeError("N=1000 point unexpectedly exceeds 30 minutes; requires background controller")
        timeout = max(3600, 3 * predicted)
        task = dict(key=key, purposes=[phase], predicted_seconds=predicted,
                    timeout_seconds=timeout, command=[])
        now = datetime.now().astimezone()
        print(f"{phase}: N={size} ef={ef}, estimated finish=" +
              ((now + timedelta(seconds=predicted)).isoformat() if predicted else "pending calibration"), flush=True)
        publish(self.out / "status.json", dict(state="running", scope=list(SIZES), N=size, ef=ef,
                estimated_at=now.isoformat(), estimated_remaining_seconds=predicted or None,
                estimated_finish_at=(now + timedelta(seconds=predicted)).isoformat() if predicted else None,
                timezone=now.tzname()))
        for path in (index, query, gt):
            if digest(path) != self.inputs[path.name]:
                raise ValueError(f"input changed: {path}")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(dir=directory, prefix="pending_") as temporary:
            pending = Path(temporary)
            command = [ROOT / "build/baseline1_experiment", "--transport", "mmap", "--index", index,
                       "--queries", query, "--gt", gt, "--read-strategy", "prefix",
                       "--prefix-granularity", 32, "--ef-search", ef, "--k", 10,
                       "--cache-capacity", 100000, "--warmup", 0, "--ef-construction", 200,
                       "--seed", 42, "--dataset", "synthetic", "--workload", "query",
                       "--label", f"n{size}_g32_ef{ef}_r0", "--out", pending / "per_query.csv",
                       "--summary-out", pending / "summary.csv"]
            task["command"] = list(map(str, command))
            publish(directory / "task.json", task)
            with (directory / "stdout.log").open("w") as stdout, (directory / "stderr.log").open("w") as stderr:
                process = subprocess.Popen(task["command"], stdout=stdout, stderr=stderr)
                publish(directory / "process.json", dict(pid=process.pid, pgid=os.getpgid(process.pid),
                        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                        starttime=Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]))
                try:
                    code = process.wait(timeout=timeout)
                except BaseException:
                    process.kill()
                    process.wait()
                    publish(directory / "exit.json", dict(exit_code=process.returncode, interrupted=True))
                    raise
            publish(directory / "exit.json", dict(exit_code=code, elapsed_seconds=time.monotonic() - started))
            if code:
                raise RuntimeError(f"client exit {code}: {directory}")
            row = validate(pending / "summary.csv", pending / "per_query.csv", size, ef, count)
            if digest(index) != self.inputs[index.name]:
                raise ValueError("index changed during run")
            (pending / "summary.csv").replace(summary)
            (pending / "per_query.csv").replace(per_query)
        publish(marker, dict(key=key, elapsed_seconds=time.monotonic() - started,
                             outputs={path.name: digest(path) for path in (summary, per_query)}, exit_code=0))
        return row


def small_main(sizes=(1000,)):
    global SIZES
    SIZES = tuple(sizes)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    label = "_".join(f"n{size}" for size in SIZES)
    campaign = ROOT / "out/prefix_g32_efsearch" / f"campaign_{stamp}_{label}_v2"
    args = argparse.Namespace(out=campaign, data=ROOT / "data/prefix_g32_efsearch_v2" / campaign.name,
                              timeout=3600)
    experiment = SmallExperiment(args)
    experiment.identity.update(protocol_version="g32_threshold_midpoint_v2", scope=list(SIZES),
                               stopping_widths={str(k): v for k, v in STOP_WIDTH.items()})
    reference = ROOT / "out/prefix_g32_efsearch/campaign_20261106T050654+0000_n1000_v2"
    if any(size != 1000 for size in SIZES):
        reference_manifest = json.loads((reference / "manifest.json").read_text())
        reference_data = Path(reference_manifest["data"])
        for name, sha in reference_manifest["inputs"].items():
            if digest(reference_data / name) != sha:
                raise ValueError(f"reference input changed: {name}")
        experiment.identity["reference_n1000"] = dict(campaign=str(reference),
                    manifest_sha256=digest(reference / "manifest.json"),
                    inputs=reference_manifest["inputs"])
    lock = (campaign / "controller.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    publish(campaign / "controller.json", dict(pid=os.getpid(), pgid=os.getpgrp(), scope=list(SIZES),
            host=platform.node(), starttime=Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19],
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip()))
    manifest = dict(identity=experiment.identity, scope=list(SIZES), data=str(args.data),
                    python=platform.python_version(), numpy=np.__version__,
                    disk_free_bytes=shutil.disk_usage(campaign).free)
    publish(campaign / "manifest.json", manifest)
    print(f"scope={SIZES} campaign: {campaign}", flush=True)
    try:
        experiment.prepare()
        manifest["inputs"] = experiment.inputs
        publish(campaign / "manifest.json", manifest)
        if any(size != 1000 for size in SIZES):
            if digest(args.data / "queries.fvecs") != reference_manifest["inputs"]["queries.fvecs"]:
                raise ValueError("reference query mismatch")
            if not np.array_equal(read_fvecs(args.data / "base100000.fvecs")[:1000],
                                  read_fvecs(reference_data / "base1000.fvecs")):
                raise ValueError("reference base prefix mismatch")
        for size in SIZES:
            experiment.rate = 0
            rates = []
            for ef in sorted({min(value, size) for value in (64, 256, 1024, 4096)}):
                experiment.run(size, ef, phase="pilot")
                saved = json.loads((campaign / f"pilot/n{size}/ef{ef}/r0/complete.json").read_text())
                rates.append(saved["elapsed_seconds"] / ef / 20)
            experiment.rate = max(rates)
            points = {}
            def measure(ef, phase):
                row = experiment.run(size, ef, phase=phase)
                points[ef] = int(row["hits"])
                ordered = sorted(points)
                if any(points[left] > points[right] for left, right in zip(ordered, ordered[1:])):
                    raise ValueError("invalid_nonmonotonic")
                return points[ef]
            if size != 1000:
                for ef in (10, 46, 47, 48, 78, 79, 80):
                    measure(ef, "reference_n1000")
            for ef in sorted({value for value in COARSE if value <= size} | {size}):
                if points and max(points.values()) >= 9900:
                    break
                measure(ef, "coarse")
            width = STOP_WIDTH[size]
            for target in (9500, 9900):
                path = campaign / f"targets/n{size}/recall{target // 100:03d}.json"
                result = locate_target(measure, points, target, width, lambda value: publish(path, value))
                assert result["R"] - result["L"] <= width
                publish(path, dict(result, N=size, target=target / 10000, stopping_width=width))
            publish(campaign / f"N{size}_DONE.json", dict(scope=[size], exit_code=0,
                    elapsed_seconds=time.monotonic() - experiment.started,
                    note="partial scope; not full experiment completion"))
        publish(campaign / "status.json", dict(state="completed", scope=list(SIZES), full_experiment_complete=False))
        publish(campaign / "exit.json", dict(exit_code=0, finished_at=datetime.now().astimezone().isoformat()))
        print(f"scope={SIZES} measurement complete: {campaign}", flush=True)
    except BaseException as error:
        publish(campaign / "status.json", dict(state="failed", error=str(error), scope=list(SIZES)))
        publish(campaign / "exit.json", dict(exit_code=1, reason=str(error)))
        raise
    finally:
        lock.close()


def main():
    import sys
    if "--small-sizes" in sys.argv:
        small_parser = argparse.ArgumentParser()
        small_parser.add_argument("--small-sizes", type=int, nargs="+",
                                  choices=(1000, 2000, 5000, 10000), required=True)
        small_args = small_parser.parse_args()
        small_main(tuple(sorted(set(small_args.small_sizes))))
        return 0
    if "--n1000" in sys.argv:
        small_main()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data/prefix_g32_efsearch")
    parser.add_argument("--out", type=Path, default=ROOT / "out/prefix_g32_efsearch")
    parser.add_argument("--phase", choices=("prepare", "pilot", "all"), default="all")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--budget-seconds", type=float, default=1200)
    parser.add_argument("--max-estimated-seconds", type=float, default=3600)
    args = parser.parse_args()
    experiment = Experiment(args)
    import matplotlib
    manifest = dict(identity=experiment.identity, python=platform.python_version(),
                    numpy=np.__version__, matplotlib=matplotlib.__version__,
                    timeout_seconds=args.timeout, budget_seconds=args.budget_seconds,
                    max_estimated_seconds=args.max_estimated_seconds,
                    disk_free_bytes=shutil.disk_usage(args.out).free,
                    memory=Path("/proc/meminfo").read_text(),
                    git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip())
    status = "complete"
    try:
        experiment.prepare()
        manifest["inputs"] = experiment.inputs
        if args.phase != "prepare":
            experiment.scan()
    except (ValueError, TimeoutError, subprocess.SubprocessError) as error:
        status = "blocked"
        manifest["reason"] = str(error)
        print(f"BLOCKED: {error}", flush=True)
    finally:
        manifest.update(status=status, phase=args.phase, elapsed_seconds=time.monotonic() - experiment.started)
        publish(args.out / "manifest.json", manifest)
    return 0 if status == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())