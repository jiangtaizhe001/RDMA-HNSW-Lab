#!/usr/bin/env python3
"""Shared harness for the real-data RDMA campaigns.

Every campaign script (budget calibration, static/mixed/concurrency matrices)
needs the same three things, so they live here instead of being copied around:

  * ``Server`` - starts ``build/storage_server`` for one index, waits for the
    listening marker and stops it again, capturing its log;
  * ``run_driver`` - invokes ``build/baseline1_experiment`` with an explicit
    argument list, stores the command, stdout/stderr, exit code and timings,
    and returns the parsed one-row summary;
  * ``validate_run`` - the accounting checks from the previous campaigns
    (byte/operation identities, unbiased per-query totals, no failed reads for
    pure query workloads, query index sequence).

Runs are described by a ``RunSpec`` so the resume key can be derived from the
same object that produces the command line; a completed run is only reused when
its recorded key matches.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
STRATEGIES = {
    "B1": ("baseline1", 4),
    "B2": ("header_actual", 4),
    "B3": ("cached_length", 4),
}

# Names written into the summary CSV, which differ from the CLI spellings.
SUMMARY_STRATEGY = {
    "baseline1": "baseline1_fixed",
    "header_actual": "baseline2_header_first",
    "cached_length": "baseline3_cached_length",
    "prefix": "quantized_prefix",
}


def strategy_args(token: str) -> tuple[str, int]:
    """Translate a campaign config token (B1/B2/B3/G1/G4/G16/G32) to CLI args."""
    if token in STRATEGIES:
        return STRATEGIES[token]
    if token.startswith("G") and token[1:].isdigit():
        return "prefix", int(token[1:])
    raise ValueError(f"unknown configuration token: {token}")


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 22), b""):
            sha.update(block)
    return sha.hexdigest()


def read_csv(path: Path) -> list[dict]:
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError(f"duplicate/missing CSV columns: {path}")
        rows = list(reader)
    if any(None in row or None in row.values() for row in rows):
        raise ValueError(f"malformed CSV: {path}")
    return rows


def publish(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def binary_identity() -> dict[str, str]:
    names = ("baseline1_experiment", "storage_server", "hnsw_build",
             "check_prefix_index", "dump_vectors")
    return {name: digest(BUILD / name) for name in names if (BUILD / name).exists()}


def input_identity(paths: dict[str, Path]) -> dict[str, str]:
    return {name: digest(Path(path)) for name, path in paths.items()}


@dataclass
class RunSpec:
    """One driver invocation and everything needed to reproduce or resume it."""

    index: Path
    queries: Path
    out_dir: Path
    label: str
    strategy: str
    prefix_granularity: int
    ef_search: int
    cache_capacity: int
    k: int = 10
    seed: int = 42
    dataset: str = "synthetic"
    warmup: int = 0
    gt: Path | None = None
    transport: str = "rdma"
    host: str = "127.0.0.1"
    port: int = 18515
    ib_device: str = "mlx5_0"
    ib_port: int = 1
    rdma_connection: str = "ib"
    workload: str = "query"
    update_op: str | None = None
    update_ratio: float = 0.0
    updates: Path | None = None
    updates_count: int = 0
    update_distribution: str = "roundrobin"
    update_hot_size: int = 0
    ef_construction: int = 200
    identity: dict = field(default_factory=dict)

    def key(self) -> dict:
        return {
            "label": self.label, "strategy": self.strategy,
            "prefix_granularity": self.prefix_granularity,
            "ef_search": self.ef_search, "cache_capacity": self.cache_capacity,
            "k": self.k, "seed": self.seed, "warmup": self.warmup,
            "workload": self.workload, "update_op": self.update_op,
            "update_ratio": self.update_ratio, "updates_count": self.updates_count,
            "update_distribution": self.update_distribution,
            "update_hot_size": self.update_hot_size,
            "transport": self.transport, "identity": self.identity,
        }


def build_command(spec: RunSpec, per_query: Path, summary: Path,
                  updates_out: Path | None = None) -> list[str]:
    command = [str(BUILD / "baseline1_experiment"), "--transport", spec.transport,
               "--index", str(spec.index), "--queries", str(spec.queries),
               "--warmup", str(spec.warmup), "--ef-search", str(spec.ef_search),
               "--k", str(spec.k), "--seed", str(spec.seed),
               "--dataset", spec.dataset, "--label", spec.label,
               "--read-strategy", spec.strategy,
               "--prefix-granularity", str(spec.prefix_granularity),
               "--cache-capacity", str(spec.cache_capacity),
               "--ef-construction", str(spec.ef_construction),
               "--workload", spec.workload, "--out", str(per_query),
               "--summary-out", str(summary)]
    if spec.gt is not None:
        command += ["--gt", str(spec.gt)]
    if spec.transport == "rdma":
        command += ["--host", spec.host, "--port", str(spec.port),
                    "--ib-device", spec.ib_device, "--ib-port", str(spec.ib_port),
                    "--rdma-connection", spec.rdma_connection]
    if spec.workload == "mixed":
        command += ["--update-op", str(spec.update_op),
                    "--update-ratio", repr(spec.update_ratio),
                    "--updates", str(spec.updates),
                    "--updates-count", str(spec.updates_count),
                    "--update-distribution", spec.update_distribution]
        if spec.update_hot_size:
            command += ["--update-hot-size", str(spec.update_hot_size)]
        if updates_out is not None:
            command += ["--updates-out", str(updates_out)]
    return command


class Server:
    """``storage_server`` lifecycle for one index file."""

    def __init__(self, index: Path, log: Path, *, port: int = 18515,
                 bind: str = "127.0.0.1", ib_device: str = "mlx5_0",
                 ib_port: int = 1, ready_timeout: float = 600.0):
        self.index = Path(index)
        self.log = Path(log)
        self.port = port
        self.bind = bind
        self.ib_device = ib_device
        self.ib_port = ib_port
        self.ready_timeout = ready_timeout
        self.process: subprocess.Popen | None = None
        self.started_at = 0.0
        self.ready_seconds = 0.0

    def __enter__(self) -> "Server":
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.unlink(missing_ok=True)
        handle = self.log.open("w")
        started = time.monotonic()
        self.process = subprocess.Popen(
            [str(BUILD / "storage_server"), "--index", str(self.index),
             "--transport", "rdma", "--rdma-connection", "ib",
             "--bind", self.bind, "--port", str(self.port),
             "--ib-device", self.ib_device, "--ib-port", str(self.ib_port)],
            stdout=handle, stderr=subprocess.STDOUT)
        self.handle = handle
        while time.monotonic() - started < self.ready_timeout:
            if self.log.exists() and "RDMA server listening" in \
                    self.log.read_text(errors="replace"):
                self.ready_seconds = time.monotonic() - started
                self.started_at = time.time()
                return self
            if self.process.poll() is not None:
                raise RuntimeError(f"storage_server exited: {self.log.read_text()}")
            time.sleep(0.05)
        raise RuntimeError(f"storage_server did not become ready in "
                           f"{self.ready_timeout}s: {self.log}")

    def __exit__(self, *exc) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=30)
        self.handle.close()
        self.process = None


def validate_run(summary: Path, per_query: Path, spec: RunSpec,
                 expected_queries: int | None = None) -> dict:
    rows = read_csv(summary)
    samples = read_csv(per_query)
    if len(rows) != 1:
        raise ValueError(f"expected one summary row in {summary}, got {len(rows)}")
    row = rows[0]
    if row["strategy"] != SUMMARY_STRATEGY.get(spec.strategy, spec.strategy):
        raise ValueError(f"strategy mismatch: {row['strategy']} != {spec.strategy}")
    if int(row["ef_search"]) != spec.ef_search or int(row["k"]) != spec.k:
        raise ValueError("ef_search/k mismatch in summary")
    if int(row["cache_capacity"]) != spec.cache_capacity:
        raise ValueError("cache capacity mismatch in summary")
    if expected_queries is not None and int(row["queries"]) != expected_queries:
        raise ValueError(f"query count {row['queries']} != {expected_queries}")
    if len(samples) != int(row["queries"]):
        raise ValueError("summary/per-query row count mismatch")
    if [int(item["query_idx"]) for item in samples] != list(range(len(samples))):
        raise ValueError("query sequence is not 0..n-1")
    for record in [row, *samples]:
        number = lambda name: int(record[name]) if record.get(name) not in (None, "") else 0
        if number("requested_bytes") != number("useful_bytes") + number("wasted_bytes"):
            raise ValueError("byte identity failed")
        if spec.strategy == "prefix" and number("read_ops") != \
                number("prefix_reads") + number("fallbacks"):
            raise ValueError("prefix read_ops != prefix_reads + fallbacks")
        if spec.strategy == "cached_length" and number("fallbacks") and number("failed_reads"):
            raise ValueError("cached_length run reported failed reads")
    if spec.workload == "query":
        if int(row["failed_reads"]) or int(row["update_failures"]):
            raise ValueError("failed reads/updates in a query-only run")
        if int(row["updates"]) != 0:
            raise ValueError("query-only run reported updates")
    for name in ("requested_bytes", "useful_bytes", "wasted_bytes", "read_ops"):
        if int(row[name]) != sum(int(item[name]) for item in samples):
            raise ValueError(f"summary/per-query {name} mismatch")
    if spec.gt is not None:
        hits = round(float(row["recall"]) * int(row["queries"]) * spec.k)
        if abs(float(row["recall"]) - hits / (int(row["queries"]) * spec.k)) > 5e-7:
            raise ValueError("recall precision mismatch")
        row = dict(row, hits=hits)
    return row


def run_driver(spec: RunSpec, *, expected_queries: int | None = None,
               timeout: float | None = None) -> dict:
    """Run one driver invocation into ``spec.out_dir`` with resume + evidence."""
    out = Path(spec.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = out / "summary.csv"
    per_query = out / "per_query.csv"
    marker = out / "completed.json"
    updates_out = out / "updates.csv" if spec.workload == "mixed" else None
    key = spec.key()
    if marker.exists():
        saved = json.loads(marker.read_text())
        if saved.get("key") != key:
            raise ValueError(f"stale run in {out}: use a new directory")
        return saved["row"]
    command = build_command(spec, per_query, summary, updates_out)
    (out / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    started = time.monotonic()
    with (out / "stdout.log").open("w") as stdout, (out / "stderr.log").open("w") as stderr:
        completed = subprocess.run(command, stdout=stdout, stderr=stderr,
                                   timeout=timeout)
    elapsed = time.monotonic() - started
    meta = {"command": command, "exit_code": completed.returncode,
            "started_at": started_at, "elapsed_seconds": round(elapsed, 3),
            "key": key}
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    if completed.returncode != 0:
        raise RuntimeError(f"driver failed ({completed.returncode}) for {spec.label}; "
                           f"see {out}/stderr.log")
    row = validate_run(summary, per_query, spec, expected_queries)
    publish(marker, {"key": key, "row": row, "elapsed_seconds": round(elapsed, 3)})
    return row


def wait_until(epoch_seconds: float) -> None:
    while time.time() < epoch_seconds:
        time.sleep(0.002)


def query_sequence(path: Path) -> int:
    """Number of queries in a .fvecs file (record count)."""
    size = Path(path).stat().st_size
    with Path(path).open("rb") as stream:
        dim = int.from_bytes(stream.read(4), "little", signed=True)
    record = 4 * (dim + 1)
    if size % record:
        raise ValueError(f"malformed fvecs file: {path}")
    return size // record


def machine_facts() -> dict:
    facts = {"hostname": os.uname().nodename, "kernel": os.uname().release,
             "python": __import__("sys").version.split()[0],
             "cpu_count": os.cpu_count()}
    for name in ("/proc/meminfo",):
        if Path(name).exists():
            for line in Path(name).read_text().splitlines():
                if line.startswith("MemTotal"):
                    facts["mem_total_kb"] = int(line.split()[1])
    # Frequency policy and huge-page policy are the two settings that produced
    # run-to-run latency states in this campaign, so record them with every
    # manifest instead of rediscovering them later.
    for key, name in (("cpu_governor",
                       "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
                      ("transparent_hugepage",
                       "/sys/kernel/mm/transparent_hugepage/enabled"),
                      ("thp_defrag",
                       "/sys/kernel/mm/transparent_hugepage/defrag")):
        candidate = Path(name)
        if candidate.exists():
            try:
                facts[key] = candidate.read_text().strip()
            except OSError:
                pass
    return facts
