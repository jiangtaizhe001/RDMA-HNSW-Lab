#!/usr/bin/env python3
"""Run the isolated 10K batch of g32_threshold_midpoint_v2."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import platform
import signal
import shutil
import struct
import subprocess
import sys
import tempfile
import time

import numpy as np

from compute_gt import exact_gt, read_fvecs
from run_prefix_g32_efsearch import COARSE, ROOT, STOP_WIDTH, digest, locate_target, publish, validate

OUTPUT = ROOT / "out/prefix_g32_efsearch"
SOURCE = ROOT / "data/prefix_g32_efsearch_v2/campaign_20261107T063740+0000_n10000_v2"


def now():
    return datetime.now().astimezone().isoformat()


def process_identity(pid):
    return dict(pid=pid, pgid=os.getpgid(pid), host=platform.node(),
                boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                starttime=Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def heartbeat(campaign, parent):
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    expected = json.loads((campaign / "controller.json").read_text())
    while True:
        try:
            if process_identity(parent) != {key: expected[key] for key in process_identity(parent)}:
                return
            status = json.loads((campaign / "status.json").read_text())
            publish(campaign / "heartbeat.json", dict(updated_at=now(), monitor=process_identity(os.getpid()),
                    current=status.get("current")))
            signal.sigtimedwait({signal.SIGUSR1}, 15)
        except (FileNotFoundError, ProcessLookupError):
            return


class Batch:
    def __init__(self, campaign):
        self.campaign = campaign
        self.size = 10000
        self.source = SOURCE
        self.points = {}
        self.rate = 0
        self.status = dict(state="preparing", scope=[10000], full_experiment_complete=False,
                           targets={f"n{size}/recall{target}": {"state": "pending"}
                                    for size in STOP_WIDTH for target in ("095", "099")})
        self.status["full_experiment_estimate"] = dict(estimated_finish_at=None,
                reason="Other sizes are outside this batch; their remaining work is not calibrated.")

    def update(self, **values):
        self.status.update(values, last_progress_at=now(), completed_points=len(self.points))
        publish(self.campaign / "status.json", self.status)

    def prepare(self):
        saved = json.loads((SOURCE / "prepared.json").read_text())
        for name, sha in saved["hashes"].items():
            if digest(SOURCE / name) != sha:
                raise ValueError(f"input hash mismatch: {name}")
        for name, sha in saved["identity"]["files"].items():
            if name.startswith("build/") and digest(ROOT / name) != sha:
                raise ValueError(f"generator/build binary changed: {name}")
        base = read_fvecs(SOURCE / "base10000.fvecs")
        queries = read_fvecs(SOURCE / "queries.fvecs")
        full_base = read_fvecs(SOURCE / "base100000.fvecs")
        if base.shape != (10000, 128) or queries.shape != (1000, 128):
            raise ValueError("input dimensions/count mismatch")
        if full_base.shape != (100000, 128) or not np.array_equal(base, full_base[:10000]):
            raise ValueError("base prefix mismatch")
        for name, matrix in (("base10000.fvecs", base), ("queries.fvecs", queries)):
            if np.max(np.abs(matrix)) > 1:
                raise ValueError("synthetic values outside [-1,1]")
            with (SOURCE / name).open("rb") as stream:
                for index in range(3):
                    values = struct.unpack("<i128f", stream.read(516))
                    if values[0] != 128 or not np.array_equal(matrix[index], values[1:]):
                        raise ValueError("independent decode failed")
        reference = saved["identity"]["reference_n1000"]
        reference_manifest = Path(reference["campaign"]) / "manifest.json"
        if digest(reference_manifest) != reference["manifest_sha256"]:
            raise ValueError("reference provenance mismatch")
        if saved["hashes"]["queries.fvecs"] != reference["inputs"]["queries.fvecs"]:
            raise ValueError("reference query mismatch")
        reference_data = Path(json.loads(reference_manifest.read_text())["data"])
        if not np.array_equal(base[:1000], read_fvecs(reference_data / "base1000.fvecs")):
            raise ValueError("reference base mismatch")
        gt, checks = exact_gt(base, queries)
        if (SOURCE / "gt10000.u32").stat().st_size != 40000:
            raise ValueError("GT row count mismatch")
        if not np.array_equal(gt, np.fromfile(SOURCE / "gt10000.u32", dtype="<u4").reshape(1000, 10)):
            raise ValueError("full recomputed GT mismatch")
        if not np.array_equal(read_fvecs(SOURCE / "pilot.fvecs"), queries[:20]):
            raise ValueError("pilot queries mismatch")
        if (SOURCE / "pilot_gt10000.u32").read_bytes() != gt[:20].tobytes():
            raise ValueError("pilot GT mismatch")
        import matplotlib
        self.identity = dict(saved["identity"], scope=[10000], stopping_widths=STOP_WIDTH,
                files={name: digest(ROOT / name) for name in [*saved["identity"]["files"],
                                                            "scripts/run_prefix_g32_10k.py"]})
        self.identity = json.loads(json.dumps(self.identity))
        manifest = dict(identity=self.identity, data=str(SOURCE), inputs=saved["hashes"],
                input_provenance=str(SOURCE / "prepared.json"), provenance_sha256=digest(SOURCE / "prepared.json"),
                gt_checks=checks, python=platform.python_version(), numpy=np.__version__,
                matplotlib=matplotlib.__version__, memory=Path("/proc/meminfo").read_text(),
                disk_free_bytes=shutil.disk_usage(self.campaign).free)
        manifest_path = self.campaign / "manifest.json"
        if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
            previous = json.loads(manifest_path.read_text())
            if previous["identity"] != self.identity or previous["inputs"] != saved["hashes"]:
                raise ValueError("resume identity mismatch")
        else:
            publish(manifest_path, manifest)
        self.inputs = saved["hashes"]

    def measure(self, ef, purpose):
        pilot = purpose == "pilot"
        count = 20 if pilot else 1000
        directory = self.campaign / ("pilot" if pilot else "runs") / f"n{self.size}" / f"ef{ef}" / "r0"
        directory.mkdir(parents=True, exist_ok=True)
        paths = [self.source / f"index{self.size}.bin", self.source / ("pilot.fvecs" if pilot else "queries.fvecs"),
             self.source / (f"pilot_gt{self.size}.u32" if pilot else f"gt{self.size}.u32")]
        key = dict(identity=self.identity, N=self.size, ef=ef, repeat=0, count=count,
                   inputs={path.name: self.inputs[path.name] for path in paths})
        marker = directory / "complete.json"
        if marker.exists():
            complete = json.loads(marker.read_text())
            if complete["key"] != key or complete["exit_code"] != 0:
                raise ValueError("invalid resume key")
            for name, sha in complete["outputs"].items():
                if digest(directory / name) != sha:
                    raise ValueError("output hash mismatch")
            row = validate(directory / "summary.csv", directory / "per_query.csv", self.size, ef, count)
            task = json.loads((directory / "task.json").read_text())
            task["purposes"] = sorted(set(task["purposes"] + [purpose]))
            publish(directory / "task.json", task)
        else:
            for path in paths:
                if digest(path) != self.inputs[path.name]:
                    raise ValueError(f"input changed: {path}")
            attempts = directory / "attempts"
            attempts.mkdir(exist_ok=True)
            attempt = attempts / f"attempt_{time.time_ns()}"
            attempt.mkdir()
            prediction = self.rate * ef * count * 1.5 if self.rate else None
            timeout = max(3600, 3 * (prediction or 0))
            timestamp = datetime.now().astimezone()
            estimate = dict(estimated_at=timestamp.isoformat(), estimated_remaining_seconds=prediction,
                    estimated_finish_at=(timestamp + timedelta(seconds=prediction)).isoformat() if prediction else None,
                    timezone=timestamp.tzname(), basis="max observed seconds/query/ef * 1.5")
            if self.rate:
                upper = self.rate * 1000 * 1.5 * (sum(value for value in COARSE if value <= self.size) + self.size + 2 * 15 * self.size)
                estimate["batch_estimate"] = dict(estimated_remaining_seconds_range=[0, upper],
                        estimated_finish_at_range=[timestamp.isoformat(), (timestamp + timedelta(seconds=upper)).isoformat()],
                        coverage=f"N={self.size} coarse and up to 15 bisections/midpoints per target; conservative, not guarantee")
            self.update(state="running", current=dict(N=self.size, ef=ef, purpose=purpose), **estimate)
            if not (self.campaign / "initial_prediction.json").exists() and self.rate:
                publish(self.campaign / "initial_prediction.json", estimate)
            command = [str(ROOT / "build/baseline1_experiment"), "--transport", "mmap", "--index", str(paths[0]),
                    "--queries", str(paths[1]), "--gt", str(paths[2]), "--read-strategy", "prefix",
                    "--prefix-granularity", "32", "--ef-search", str(ef), "--k", "10", "--cache-capacity", "100000",
                    "--warmup", "0", "--ef-construction", "200", "--seed", "42", "--dataset", "synthetic",
                    "--workload", "query", "--label", f"n{self.size}_g32_ef{ef}_r0", "--out", str(attempt / "per_query.csv"),
                    "--summary-out", str(attempt / "summary.csv")]
            publish(directory / "task.json", dict(key=key, purposes=[purpose], command=command,
                    timeout_seconds=timeout, **estimate))
            started = time.monotonic()
            with (attempt / "stdout.log").open("w") as stdout, (attempt / "stderr.log").open("w") as stderr:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
                publish(directory / "process.json", dict(process_identity(process.pid), started_at=now(), command=command))
                try:
                    code = process.wait(timeout=timeout)
                except BaseException:
                    process.kill()
                    process.wait()
                    publish(directory / "exit.json", dict(exit_code=process.returncode, reason="timeout or interrupted", finished_at=now()))
                    raise
            elapsed = time.monotonic() - started
            publish(directory / "exit.json", dict(exit_code=code, elapsed_seconds=elapsed, finished_at=now()))
            if code:
                raise RuntimeError(f"client exited {code}")
            row = validate(attempt / "summary.csv", attempt / "per_query.csv", self.size, ef, count)
            for path in paths:
                if digest(path) != self.inputs[path.name]:
                    raise ValueError("input changed during run")
            for name in ("summary.csv", "per_query.csv", "stdout.log", "stderr.log"):
                shutil.copyfile(attempt / name, directory / name)
            complete = dict(key=key, exit_code=0, elapsed_seconds=elapsed,
                    outputs={name: digest(directory / name) for name in ("summary.csv", "per_query.csv")})
            publish(marker, complete)
        self.rate = max(self.rate, complete["elapsed_seconds"] / ef / count)
        if not pilot:
            self.points[ef] = int(row["hits"])
            ordered = sorted(self.points)
            if any(self.points[left] > self.points[right] for left, right in zip(ordered, ordered[1:])):
                raise ValueError("invalid_nonmonotonic")
        self.update()
        print(f"{now()} {purpose} ef={ef} recall={row['recall']}", flush=True)
        return int(row["hits"])

    def scan(self):
        self.prepare()
        for ef in (64, 256, 1024, 4096):
            self.measure(ef, "pilot")
        for ef in sorted({value for value in COARSE if value <= 10000} | {10000}):
            self.measure(ef, "coarse")
            if max(self.points.values()) >= 9900:
                break
        for target in (9500, 9900):
            path = self.campaign / f"targets/n10000/recall{target // 100:03d}.json"
            def checkpoint(value):
                publish(path, dict(value, N=10000, target=target / 10000, stopping_width=4,
                                   stopping_widths=STOP_WIDTH, identity=self.identity))
                self.update(pending_target=target)
            previous = json.loads(path.read_text()) if path.exists() else {}
            if previous.get("state") in ("estimated", "at_minimum", "unreached"):
                if previous["identity"] != self.identity:
                    raise ValueError("target resume identity mismatch")
                result = {key: value for key, value in previous.items()
                          if key not in ("N", "target", "stopping_width", "stopping_widths", "identity")}
                for ef in (result.get("L"), result.get("R"), result.get("estimate")):
                    if ef is not None:
                        self.measure(ef, "resume_evidence")
            else:
                result = locate_target(self.measure, self.points, target, 4, checkpoint)
            if result["state"] == "unreached" and 10000 not in self.points:
                raise ValueError("unreached without ef=N evidence")
            checkpoint(result)
            self.status["targets"][f"n10000/recall{target // 100:03d}"] = result
            self.update()
        for name, sha in self.inputs.items():
            if digest(SOURCE / name) != sha:
                raise ValueError("final input hash mismatch")
        evidence = {str(path.relative_to(self.campaign)): digest(path)
                    for path in (self.campaign / "targets/n10000").glob("*.json")}
        publish(self.campaign / "N10000_DONE.json", dict(exit_code=0, scope=[10000],
                full_experiment_complete=False, finished_at=now(), evidence=evidence,
                note="No cross-size analysis until all five sizes finish."))
        self.update(state="completed", current=None, estimated_remaining_seconds=0, estimated_finish_at=now())


def control(campaign):
    lock = (campaign / "controller.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    batch = Batch(campaign)
    batch.update()
    publish(campaign / "controller.json", dict(process_identity(os.getpid()), started_at=now(), command=sys.argv))
    monitor = subprocess.Popen([sys.executable, __file__, "--heartbeat", str(campaign), "--parent", str(os.getpid())],
                               stdin=subprocess.DEVNULL)
    def interrupt(signum, frame):
        raise InterruptedError(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    code, reason = 0, "10K batch completed; full experiment not complete"
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
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--campaign", type=Path)
    parser.add_argument("--heartbeat", type=Path)
    parser.add_argument("--parent", type=int)
    args = parser.parse_args()
    if args.heartbeat:
        heartbeat(args.heartbeat, args.parent)
    elif args.launch:
        campaign = args.campaign or OUTPUT / ("campaign_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + digest(__file__)[:12] + "_n10000")
        campaign = campaign.resolve()
        if campaign.parent != OUTPUT:
            raise ValueError("campaign must be under repository output root")
        campaign.mkdir(parents=True, exist_ok=True)
        with (campaign / "controller.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (campaign / "controller.log").open("a") as stream:
            process = subprocess.Popen(["nohup", sys.executable, __file__, "--campaign", str(campaign)],
                    stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True)
        for attempt in range(100):
            if (campaign / "controller.json").exists():
                saved = json.loads((campaign / "controller.json").read_text())
                if saved["pid"] == process.pid:
                    print(json.dumps(dict(campaign=str(campaign), pid=process.pid,
                        message="Background started; point/batch ETA pending pilot calibration; full experiment ETA unavailable outside 10K scope.",
                        status=str(campaign / "status.json")), indent=2))
                    return
            if process.poll() is not None:
                raise RuntimeError(f"controller failed to start: {campaign}")
            time.sleep(0.05)
        raise RuntimeError("controller startup not confirmed")
    elif args.campaign:
        control(args.campaign.resolve())
    else:
        parser.error("use --launch or --campaign")


if __name__ == "__main__":
    main()