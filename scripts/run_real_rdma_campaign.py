#!/usr/bin/env python3
"""Run the real-data RDMA campaign matrices (P1 quality/static/capacity, P2 mixed, P3 concurrency).

The script is parameterised by dataset, scale, index and input hashes; nothing
about synthetic data, 100K or ef_search=64 is hard coded.  Every run gets its
own directory with the command, stdout/stderr, exit code, timings, summary and
per-query CSV, plus a ``completed.json`` resume marker whose key covers the
binaries, inputs and every parameter (section 10 of
`docs/real-data-rdma-improvement-plan.md`).

Per-run protocol (section 6):

  * cold queries are the first ``--cold`` rows of the run, warm-up is the next
    ``--warmup`` rows, the timed window is everything after that; all three live
    in one client process so the cache state is continuous and the window
    boundaries are fixed before the run, not chosen afterwards;
  * the timed window holds at least ``--min-timed`` queries *and* at least
    ``--min-seconds`` seconds: the query count is derived from the mean latency
    measured by the quality phase and is therefore identical for every
    configuration of the same cell;
  * the query file is the fixed timing split repeated cyclically; repetitions
    are recorded in the cycle manifest so loops are never counted as new
    independent queries;
  * the server is restarted for every run.

Usage
-----
    python3 scripts/run_real_rdma_campaign.py \
        --campaign sift_n100k --dataset sift --n 100000 \
        --index data/real/sift/index_n100000_M16_seed42.bin \
        --timing-queries data/real/sift/sift_query_timing.fvecs \
        --quality-queries data/real/sift/sift_query_quality.fvecs \
        --quality-gt data/real/sift/sift_gt_n100000_quality_k10.u32 \
        --calibration out/real_data_rdma/sift/n100000/calibration.json \
        --targets 0.95,0.99 --reps 5 --phases quality static
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time

import numpy as np

from experiment_harness import (BUILD, ROOT, RunSpec, Server, binary_identity,
                                digest, input_identity, machine_facts, publish,
                                query_sequence, read_csv, run_driver,
                                strategy_args, wait_until)
from compute_gt import read_fvecs


def cycle_queries(source: Path, count: int, out: Path, spec: dict) -> Path:
    """Write ``count`` queries by repeating ``source``; record the repetition."""
    if out.exists():
        saved = json.loads(out.with_suffix(".json").read_text())
        if any(saved.get(key) != value for key, value in spec.items()):
            raise ValueError(f"cycle file {out} was built from different inputs")
        return out
    if count % 1 or count <= 0:
        raise ValueError("count must be positive")
    data = read_fvecs(source)
    reps = (count + len(data) - 1) // len(data)
    block = np.tile(data, (reps, 1))[:count]
    records = np.empty((count, data.shape[1] + 1), dtype="<i4")
    records[:, 0] = data.shape[1]
    records[:, 1:] = block.view("<i4")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(".fvecs.tmp")
    records.tofile(temporary)
    temporary.replace(out)
    publish(out.with_suffix(".json"), dict(spec, path=str(out), count=count,
                                           repetitions=reps,
                                           source_sha256=digest(source)))
    return out


def shard_queries(source: Path, count: int, shard: int, shards: int,
                  out: Path, spec: dict) -> Path:
    """Disjoint per-client query sequence: take every ``shards``-th query."""
    if out.exists():
        saved = json.loads(out.with_suffix(".json").read_text())
        if any(saved.get(key) != value for key, value in spec.items()):
            raise ValueError(f"shard file {out} was built from different inputs")
        return out
    data = read_fvecs(source)
    subset = data[shard::shards]
    if not len(subset):
        raise ValueError("empty shard")
    reps = (count + len(subset) - 1) // len(subset)
    block = np.tile(subset, (reps, 1))[:count]
    records = np.empty((count, data.shape[1] + 1), dtype="<i4")
    records[:, 0] = data.shape[1]
    records[:, 1:] = block.view("<i4")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(".fvecs.tmp")
    records.tofile(temporary)
    temporary.replace(out)
    publish(out.with_suffix(".json"), dict(spec, path=str(out), count=count,
                                           shard=shard, shards=shards,
                                           repetitions=reps,
                                           source_sha256=digest(source)))
    return out


class Campaign:
    def __init__(self, args):
        self.args = args
        self.root = args.out_dir / args.campaign
        self.root.mkdir(parents=True, exist_ok=True)
        self.started = time.monotonic()
        self.status = {"completed": [], "failed": [], "skipped": [],
                       "pending": []}
        self.configs = [token.strip() for token in args.configs.split(",") if token.strip()]
        self.budgets = self.load_budgets()
        self.identity = dict(
            binaries=binary_identity(),
            inputs=input_identity({"index": args.index,
                                   "timing_queries": args.timing_queries,
                                   "quality_queries": args.quality_queries,
                                   "quality_gt": args.quality_gt,
                                   **({"updates": args.updates}
                                      if args.updates is not None else {})}),
            dataset=args.dataset, n=args.n, k=args.k, seed=args.seed,
            cache_capacity=args.cache, warmup=args.warmup, cold=args.cold,
            min_timed=args.min_timed, min_seconds=args.min_seconds,
            window_margin=args.window_margin,
            ef_construction=args.ef_construction, budgets=self.budgets,
            transport=args.transport, reps=args.reps,
            clients=[int(token) for token in str(args.clients).split(",")
                     if token.strip()])
        publish(self.root / "manifest.json",
                {"identity": self.identity, "machine": machine_facts(),
                 "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "argv": __import__("sys").argv,
                 "protocol": {"windows": ["cold", "warmup", "timed"],
                              "server_restart_per_run": True,
                              "timing_queries": "fixed split repeated cyclically"}})

    def load_budgets(self) -> dict:
        if not self.args.calibration:
            return {"0.95": int(self.args.budgets.split(",")[0])}
        data = json.loads(Path(self.args.calibration).read_text())
        budgets = {}
        for target in self.args.targets.split(","):
            entry = data["targets"].get(f"{float(target):.2f}")
            if not entry or entry.get("state") != "found":
                raise SystemExit(f"calibration has no passing budget for {target}")
            budgets[f"{float(target):.2f}"] = int(entry["R"])
        if data["identity"]["inputs"]["index"] != digest(self.args.index):
            raise SystemExit("calibration index hash does not match --index")
        return budgets

    # -- helpers ---------------------------------------------------------
    def left(self) -> float:
        return self.args.max_seconds - (time.monotonic() - self.started)

    def check_budget(self) -> None:
        if self.left() <= 0:
            raise TimeoutError("campaign --max-seconds exhausted; rerun to resume")

    def spec(self, *, out: Path, label: str, queries: Path, ef: int, config: str,
             cache: int, gt: Path | None = None, warmup: int = 0,
             workload: str = "query", index: Path | None = None,
             update_op: str | None = None, update_ratio: float = 0.0,
             updates: Path | None = None, updates_count: int = 0) -> RunSpec:
        strategy, granularity = strategy_args(config)
        return RunSpec(index=index or self.args.index, queries=queries,
                       out_dir=out, label=label, strategy=strategy,
                       prefix_granularity=granularity, ef_search=ef,
                       cache_capacity=cache, k=self.args.k, seed=self.args.seed,
                       dataset=self.args.dataset, warmup=warmup, gt=gt,
                       transport=self.args.transport, host=self.args.host,
                       port=self.args.port, ib_device=self.args.ib_device,
                       ib_port=self.args.ib_port,
                       ef_construction=self.args.ef_construction,
                       workload=workload, update_op=update_op,
                       update_ratio=update_ratio, updates=updates,
                       updates_count=updates_count,
                       identity=self.identity)

    def run_server(self, index: Path, log: Path):
        return Server(index, log, port=self.args.port,
                      ib_device=self.args.ib_device, ib_port=self.args.ib_port)

    @property
    def run_timeout(self) -> float | None:
        return self.args.run_timeout_s if self.args.run_timeout_s > 0 else None

    # -- phases ----------------------------------------------------------
    def phase_quality(self) -> dict:
        """Quality gate per (target, config): independent split, precomputed GT."""
        latencies = {}
        for target, ef in sorted(self.budgets.items()):
            for config in self.configs:
                self.check_budget()
                out = self.root / "runs/quality" / f"recall{target}_c{self.args.cache}" \
                    / config
                label = f"{self.args.dataset}_n{self.args.n}_q{target}_c{self.args.cache}_{config}"
                spec = self.spec(out=out, label=label,
                                 queries=self.args.quality_queries, ef=ef,
                                 config=config, cache=self.args.cache,
                                 gt=self.args.quality_gt)
                if (out / "completed.json").exists():
                    row = json.loads((out / "completed.json").read_text())["row"]
                else:
                    with self.run_server(self.args.index, out / "server.log"):
                        row = run_driver(
                            spec,
                            expected_queries=query_sequence(self.args.quality_queries),
                            timeout=self.run_timeout)
                    self.status["completed"].append(str(out.relative_to(self.root)))
                key = f"{target}/{config}"
                latencies[key] = float(row["latency_mean_us"])
                print(f"quality {target} {config}: recall={float(row['recall']):.5f} "
                      f"reads={row['read_ops']} p50={row['latency_p50_us']}us", flush=True)
        publish(self.root / "quality_latency.json", latencies)
        return latencies

    def timed_count(self, latencies: dict) -> int:
        """Query count that keeps every configuration above --min-seconds.

        The count is shared by every configuration of a cell, so it has to be
        sized from the *fastest* mean latency; sizing it from the slowest (as
        the first SIFT 100K matrix did) leaves the fastest configuration with a
        window shorter than the 60 s floor.  ``--window-margin`` adds headroom
        for run-to-run state drift: with the old 1.05 factor several 1M cells
        landed at 56.9-58.2 s because the actual repetitions were ~8 % slower
        than the quality-phase mean they were sized from.
        """
        fastest = min(latencies.values()) if latencies else 5000.0
        seconds_needed = (self.args.min_seconds * self.args.window_margin
                          / (fastest / 1e6))
        count = int(np.ceil(seconds_needed / 1000.0) * 1000)
        return max(self.args.min_timed, count)

    def timed_seconds(self, out: Path) -> float:
        rows = read_csv(Path(out) / "per_query.csv")
        start = self.args.cold + self.args.warmup
        return sum(float(row["latency_us"]) for row in rows[start:]) / 1e6

    def phase_static(self, phase: str, caches: list[int], reps: int) -> None:
        latencies = {}
        latency_file = self.root / "quality_latency.json"
        if latency_file.exists():
            latencies = json.loads(latency_file.read_text())
        if not latencies:
            raise SystemExit("run the quality phase first: it fixes the timed query count")
        total = self.args.cold + self.args.warmup + self.timed_count(latencies)
        cycle = cycle_queries(
            self.args.timing_queries, total, self.root / f"queries/cycle_{total}.fvecs",
            {"source": str(self.args.timing_queries), "count": total,
             "cold": self.args.cold, "warmup": self.args.warmup,
             "timed": total - self.args.cold - self.args.warmup,
             "protocol": "cold+warmup+timed in one client process"})
        publish(self.root / f"{phase}_queries.json",
                {"cycle": str(cycle), "count": total,
                 "timed_queries": total - self.args.cold - self.args.warmup,
                 "derived_from_latency": latencies})
        print(f"{phase}: {total} queries per run "
              f"(cold {self.args.cold}, warmup {self.args.warmup}, "
              f"timed {total - self.args.cold - self.args.warmup})", flush=True)
        for target, ef in sorted(self.budgets.items()):
            for cache in caches:
                for rep in range(1, reps + 1):
                    order = self.configs[((rep - 1) % len(self.configs)):] + \
                        self.configs[:((rep - 1) % len(self.configs))]
                    for config in order:
                        self.check_budget()
                        cell = f"runs/{phase}/n{self.args.n}_recall{target}_c{cache}"
                        out = self.root / cell / config / f"r{rep}"
                        label = (f"{self.args.dataset}_n{self.args.n}_{phase}"
                                 f"_q{target}_c{cache}_{config}_r{rep}")
                        spec = self.spec(out=out, label=label, queries=cycle,
                                         ef=ef, config=config, cache=cache)
                        if (out / "completed.json").exists():
                            self.status["skipped"].append(str(out.relative_to(self.root)))
                            continue
                        print(f"{phase} {target} c{cache} {config} r{rep}", flush=True)
                        try:
                            with self.run_server(self.args.index, out / "server.log"):
                                run_driver(spec, expected_queries=total,
                                           timeout=self.run_timeout)
                            window = self.timed_seconds(out)
                            if window < self.args.min_seconds:
                                self.status.setdefault("short_windows", []).append(
                                    {"run": str(out.relative_to(self.root)),
                                     "timed_seconds": round(window, 1),
                                     "required": self.args.min_seconds})
                                print(f"  WARNING: timed window {window:.1f}s < "
                                      f"{self.args.min_seconds}s", flush=True)
                            self.status["completed"].append(
                                str(out.relative_to(self.root)))
                        except Exception as error:  # noqa: BLE001 - recorded below
                            self.status["failed"].append(
                                {"run": str(out.relative_to(self.root)),
                                 "error": repr(error)})
                            print(f"  FAILED: {error}", flush=True)
                            raise

    def phase_mixed(self, reps: int, ratios: list[float], ops: list[str],
                    updates_count: int) -> None:
        latencies = json.loads((self.root / "quality_latency.json").read_text())
        timed = self.timed_count(latencies)
        total = self.args.cold + self.args.warmup + timed
        cycle = cycle_queries(
            self.args.timing_queries, total, self.root / f"queries/cycle_{total}.fvecs",
            {"source": str(self.args.timing_queries), "count": total,
             "cold": self.args.cold, "warmup": self.args.warmup,
             "timed": timed, "protocol": "cold+warmup+timed in one client process"})
        publish(self.root / "mixed_queries.json",
                {"cycle": str(cycle), "count": total, "cold": self.args.cold,
                 "warmup": self.args.warmup, "timed_queries": timed,
                 "updates": str(self.args.updates),
                 "updates_count": updates_count,
                 "update_ratios": ratios, "update_ops": ops,
                 "protocol": ("query/update interleaved serially; query latency "
                              "reported separately from update latency")})
        work_dir = self.root / "work"
        for target in sorted(self.budgets):
            ef = self.budgets[target]
            for ratio in ratios:
                for op in ops:
                    for rep in range(1, reps + 1):
                        for config in self.configs:
                            self.check_budget()
                            cell = f"runs/mixed/n{self.args.n}_recall{target}_{op}{ratio}"
                            out = self.root / cell / config / f"r{rep}"
                            label = (f"{self.args.dataset}_n{self.args.n}_mixed"
                                     f"_{op}{ratio}_q{target}_{config}_r{rep}")
                            index_copy = work_dir / f"{op}{ratio}_{config}_r{rep}.bin"
                            index_copy.parent.mkdir(parents=True, exist_ok=True)
                            spec = self.spec(
                                out=out, label=label, queries=cycle, ef=ef,
                                config=config, cache=self.args.cache,
                                workload="mixed", index=index_copy,
                                update_op=op, update_ratio=ratio,
                                updates=self.args.updates,
                                updates_count=updates_count)
                            if (out / "completed.json").exists():
                                self.status["skipped"].append(
                                    str(out.relative_to(self.root)))
                                continue
                            shutil.copyfile(self.args.index, index_copy)
                            print(f"mixed {op} r={ratio} {config} rep{rep}", flush=True)
                            with self.run_server(index_copy, out / "server.log"):
                                run_driver(spec, expected_queries=total,
                                           timeout=self.run_timeout)
                            self.status["completed"].append(
                                str(out.relative_to(self.root)))
                            index_copy.unlink()

    def phase_concurrency(self, reps: int, client_counts: list[int]) -> None:
        latencies = json.loads((self.root / "quality_latency.json").read_text())
        timed = self.timed_count(latencies)
        total = self.args.cold + self.args.warmup + timed
        per_client = total
        publish(self.root / "concurrency_queries.json",
                {"count": total, "cold": self.args.cold,
                 "warmup": self.args.warmup, "timed_queries": timed,
                 "clients": client_counts,
                 "sharding": "disjoint every-k-th query of the timing split",
                 "protocol": ("barrier-synchronised start, shared measurement "
                              "window = min client wall time, server restarted "
                              "per group")})
        for clients in client_counts:
          for target in sorted(self.budgets):
            ef = self.budgets[target]
            for rep in range(1, reps + 1):
                for config in self.configs:
                    self.check_budget()
                    cell = f"runs/concurrency/n{self.args.n}_recall{target}_c{clients}"
                    group = self.root / cell / config / f"r{rep}"
                    groups_csv = self.root / cell / "groups.csv"
                    if (group / "group.json").exists():
                        self.status["skipped"].append(str(group.relative_to(self.root)))
                        continue
                    group.mkdir(parents=True, exist_ok=True)
                    shards = []
                    for client in range(clients):
                        shards.append(shard_queries(
                            self.args.timing_queries, per_client, client, clients,
                            self.root / f"queries/cycle_{total}_c{clients}_{client}.fvecs",
                            {"source": str(self.args.timing_queries), "count": total,
                             "shard": client, "shards": clients,
                             "cold": self.args.cold, "warmup": self.args.warmup,
                             "timed": timed,
                             "protocol": "cold+warmup+timed in one client process"}))
                    print(f"concurrency q{target} {config} r{rep} c{clients}", flush=True)
                    start_at = time.time() + 3.0
                    results = [None] * clients
                    errors = [None] * clients
                    barrier = threading.Barrier(clients + 1)

                    def worker(client: int) -> None:
                        try:
                            barrier.wait(timeout=30)
                            wait_until(start_at)
                            spec = self.spec(
                                out=group / f"client{client}", label=f""
                                f"{self.args.dataset}_n{self.args.n}_conc_q{target}"
                                f"_c{clients}_{config}_r{rep}_k{client}",
                                queries=shards[client], ef=ef, config=config,
                                cache=self.args.cache)
                            results[client] = run_driver(
                                spec, expected_queries=total,
                                timeout=self.run_timeout)
                        except Exception as error:  # noqa: BLE001
                            errors[client] = repr(error)

                    with self.run_server(self.args.index, group / "server.log"):
                        threads = [threading.Thread(target=worker, args=(client,))
                                   for client in range(clients)]
                        for thread in threads:
                            thread.start()
                        barrier.wait(timeout=60)
                        for thread in threads:
                            thread.join()
                    if any(errors):
                        raise RuntimeError(f"client failures: {errors}")
                    publish(group / "group.json",
                            {"clients": clients, "target": target, "config": config,
                             "rep": rep, "start_at": start_at,
                             "rows": results,
                             "estimated_window_s": min(
                                 float(row["wall_s"]) for row in results)})
                    with groups_csv.open("a") as stream:
                        if groups_csv.stat().st_size == 0:
                            stream.write("rep,config,target,clients,window_s,"
                                         "aggregate_qps,jain_fairness\n")
                        walls = [float(row["wall_s"]) for row in results]
                        window = min(walls)
                        window_queries = sum(
                            float(row["queries"]) * window / wall
                            for row, wall in zip(results, walls))
                        qps = window_queries / window
                        shares = np.array([float(row["queries"]) / wall
                                           for row, wall in zip(results, walls)])
                        jain = shares.sum() ** 2 / (len(shares) * (shares ** 2).sum())
                        stream.write(f"{rep},{config},{target},{clients},{window:.4f},"
                                     f"{qps:.3f},{jain:.6f}\n")
                    self.status["completed"].append(str(group.relative_to(self.root)))

    def phase_checkpoints(self, ops: list[str], ratios: list[float],
                          reps: int, counts: list[int], gt_dir: Path) -> None:
        """Quality gate at 0/25/50/75/100 % of a mixed round (plan section 7).

        For every ``(op, ratio, checkpoint)`` the state is replayed from a fresh
        copy of the initial index with the first ``count`` updates of the shared
        log (the driver applies exactly as many updates as the vector file
        holds).  The replay client and the quality client share one server
        session, so the quality gate sees the mutated index while the replay
        queries never enter a performance window.  The offline GT written by
        ``mixed_checkpoint_gt.py`` covers the same active set for all
        strategies, so the check is a paired comparison of recall only.
        """
        if self.args.updates is None:
            raise SystemExit("--updates is required for the checkpoints phase")
        source = read_fvecs(self.args.updates)
        work = self.root / "work"
        work.mkdir(parents=True, exist_ok=True)
        interval = {ratio: max(1, int(round(1.0 / max(0.0001, ratio))))
                    for ratio in ratios}
        publish(self.root / "checkpoint_protocol.json",
                {"counts": counts, "ops": ops, "update_ratios": ratios,
                 "reps": reps, "gt_dir": str(gt_dir),
                 "updates": str(self.args.updates),
                 "protocol": ("state replayed from the initial index copy; "
                              "quality queries are cold for every strategy; "
                              "checkpoint queries are not performance samples")})
        publish(self.root / "checkpoint_queries.json",
                {"count": query_sequence(self.args.quality_queries),
                 "cold": 0, "warmup": 0,
                 "timed": query_sequence(self.args.quality_queries),
                 "protocol": "checkpoint quality gate only; no timed window"})
        for target in sorted(self.budgets):
            ef = self.budgets[target]
            for op in ops:
                for ratio in ratios:
                    step = interval[ratio]
                    for count in counts:
                        gt = gt_dir / f"gt_{op}_c{count}_k{self.args.k}.u32"
                        if not gt.exists():
                            raise SystemExit(f"missing checkpoint GT {gt}")
                        prefix = (self.write_prefix(
                            source, count, work / f"updates_{op}_c{count}.fvecs")
                            if count else None)
                        replay_queries = max(1, count * step)
                        queries = (cycle_queries(
                            self.args.timing_queries, replay_queries,
                            self.root / f"queries/replay_{replay_queries}.fvecs",
                            {"source": str(self.args.timing_queries),
                             "count": replay_queries, "cold": 0, "warmup": 0,
                             "timed": replay_queries,
                             "protocol": "checkpoint replay, not a timed run"})
                            if count else None)
                        cell = f"checkpoint/{op}{ratio}_c{count}"
                        for config in self.configs:
                            for rep in range(1, reps + 1):
                                self.check_budget()
                                out = self.root / "runs" / cell / config / f"r{rep}"
                                label = (f"{self.args.dataset}_n{self.args.n}"
                                         f"_check_{op}{ratio}_c{count}"
                                         f"_{config}_r{rep}")
                                spec = self.spec(
                                    out=out, label=label,
                                    queries=self.args.quality_queries, ef=ef,
                                    config=config, cache=self.args.cache, gt=gt)
                                if (out / "completed.json").exists():
                                    self.status["skipped"].append(
                                        str(out.relative_to(self.root)))
                                    continue
                                index_copy = work / f"{op}{ratio}_c{count}_r{rep}.bin"
                                shutil.copyfile(self.args.index, index_copy)
                                replay = (self.spec(
                                    out=out / "replay", label=label + "_replay",
                                    queries=queries, ef=ef, config="B1",
                                    cache=self.args.cache, workload="mixed",
                                    index=index_copy, update_op=op,
                                    update_ratio=ratio, updates=prefix,
                                    updates_count=count) if count else None)
                                print(f"checkpoint {op} r={ratio} c={count} "
                                      f"{config} r{rep}", flush=True)
                                with self.run_server(index_copy, out / "server.log"):
                                    replay_row = (run_driver(
                                        replay, timeout=self.run_timeout)
                                        if replay else {})
                                    row = run_driver(
                                        spec,
                                        expected_queries=query_sequence(
                                            self.args.quality_queries),
                                        timeout=self.run_timeout)
                                index_copy.unlink()
                                publish(out / "checkpoint.json",
                                        {"op": op, "ratio": ratio,
                                         "cumulative_updates": count,
                                         "active_nodes": int(
                                             replay_row.get("node_count_end")
                                             or row.get("node_count_start") or 0),
                                         "updates_applied": int(
                                             replay_row.get("updates", 0)),
                                         "updates_failed": int(
                                             replay_row.get("update_failures", 0)),
                                         "gt": str(gt),
                                         "recall": float(row["recall"])})
                                self.status["completed"].append(
                                    str(out.relative_to(self.root)))

    @staticmethod
    def write_prefix(source, count: int, out: Path) -> Path:
        if out.exists():
            return out
        block = np.ascontiguousarray(source[:count])
        records = np.empty((len(block), block.shape[1] + 1), dtype="<i4")
        records[:, 0] = block.shape[1]
        records[:, 1:] = block.view("<i4")
        out.parent.mkdir(parents=True, exist_ok=True)
        records.tofile(out)
        return out

    def finish(self) -> None:
        publish(self.root / "status.json",
                {"status": self.status,
                 "elapsed_seconds": round(time.monotonic() - self.started, 1),
                 "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})


def main(argv) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "out/real_data_rdma")
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--n", required=True, type=int)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--timing-queries", required=True, type=Path)
    parser.add_argument("--quality-queries", required=True, type=Path)
    parser.add_argument("--quality-gt", required=True, type=Path)
    parser.add_argument("--calibration", type=Path, default=None)
    parser.add_argument("--budgets", default="64",
                        help="ef_search values when --calibration is absent")
    parser.add_argument("--targets", default="0.95,0.99")
    parser.add_argument("--configs", default="B1,B2,B3,G1,G4,G16,G32")
    parser.add_argument("--phases", default="quality static")
    parser.add_argument("--caches", default="",
                        help="capacity scan caches (default: --cache only)")
    parser.add_argument("--cache", type=int, default=100000)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--cold", type=int, default=1000)
    parser.add_argument("--warmup", type=int, default=2000)
    parser.add_argument("--min-timed", type=int, default=10000)
    parser.add_argument("--min-seconds", type=float, default=60.0)
    parser.add_argument("--window-margin", type=float, default=1.15,
                        help="headroom over --min-seconds when deriving the "
                             "shared timed query count (run-to-run drift)")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--ef-construction", type=int, default=200)
    parser.add_argument("--transport", default="rdma", choices=("rdma", "mmap"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18515)
    parser.add_argument("--ib-device", default="mlx5_0")
    parser.add_argument("--ib-port", type=int, default=1)
    parser.add_argument("--updates", type=Path, default=None)
    parser.add_argument("--updates-count", type=int, default=250)
    parser.add_argument("--mix-ratios", default="0.1,0.3,0.5")
    parser.add_argument("--mix-ops", default="erase,upsert")
    parser.add_argument("--clients", default="1,2,4,8",
                        help="comma separated client counts for the concurrency phase")
    parser.add_argument("--checkpoint-ops", default="erase,upsert")
    parser.add_argument("--checkpoint-ratios", default="0.3")
    parser.add_argument("--checkpoint-counts", default="")
    parser.add_argument("--checkpoint-reps", type=int, default=2)
    parser.add_argument("--checkpoint-gt-dir", type=Path, default=None)
    parser.add_argument("--max-seconds", type=float, default=float("inf"))
    parser.add_argument("--run-timeout-s", type=float, default=7200.0,
                        help="absolute per-run watchdog; a hung driver is killed "
                             "and recorded as a failed run instead of stalling "
                             "the pipeline (0 disables)")
    args = parser.parse_args(argv)

    campaign = Campaign(args)
    try:
        for phase in [token.strip() for token in args.phases.split(",") if token.strip()]:
            if phase == "quality":
                campaign.phase_quality()
            elif phase == "static":
                caches = [int(token) for token in args.caches.split(",") if token.strip()]
                campaign.phase_static("static", caches or [args.cache], args.reps)
            elif phase == "capacity":
                caches = [int(token) for token in args.caches.split(",") if token.strip()]
                campaign.phase_static("capacity", caches, args.reps)
            elif phase == "mixed":
                if args.updates is None:
                    raise SystemExit("--updates is required for the mixed phase")
                campaign.phase_mixed(
                    args.reps, [float(token) for token in args.mix_ratios.split(",")],
                    [token.strip() for token in args.mix_ops.split(",")],
                    args.updates_count)
            elif phase == "concurrency":
                campaign.phase_concurrency(
                    args.reps,
                    [int(token) for token in str(args.clients).split(",")
                     if token.strip()])
            elif phase == "checkpoints":
                if args.updates is None or args.checkpoint_gt_dir is None:
                    raise SystemExit("--updates and --checkpoint-gt-dir are "
                                     "required for the checkpoints phase")
                counts = [int(token) for token in
                          args.checkpoint_counts.split(",") if token.strip()]
                if not counts:
                    raise SystemExit("--checkpoint-counts is required for the "
                                     "checkpoints phase")
                campaign.phase_checkpoints(
                    [token.strip() for token in args.checkpoint_ops.split(",")],
                    [float(token) for token in args.checkpoint_ratios.split(",")],
                    args.checkpoint_reps, counts, args.checkpoint_gt_dir)
            else:
                raise SystemExit(f"unknown phase: {phase}")
    except TimeoutError as error:
        print(f"stopped early: {error}", flush=True)
        campaign.status["pending"].append(str(error))
    finally:
        campaign.finish()
    print(json.dumps({"completed": len(campaign.status["completed"]),
                      "skipped": len(campaign.status["skipped"]),
                      "failed": len(campaign.status["failed"]),
                      "pending": len(campaign.status["pending"])}))
    if campaign.status["failed"]:
        return 1
    return 2 if campaign.status["pending"] else 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
