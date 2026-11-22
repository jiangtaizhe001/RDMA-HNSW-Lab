#!/usr/bin/env python3
"""Exact ground truth for the *current* index state at a mixed checkpoint.

Section 7 of `docs/real-data-rdma-improvement-plan.md`: at 0/25/50/75/100 % of a
mixed round the quality gate has to be recomputed against the active vectors and
ids, not against the initial index.  This script replays the shared update log
produced by `prepare_update_log.py` in Python and writes one exact float64 GT
file per checkpoint state, which the campaign then evaluates on a real index that
has been replayed to the same state.

Rules (identical to the driver):

  * ``erase`` removes ``id = update_idx % n``; erased ids never come back;
  * ``upsert`` replaces the vector of ``id`` with ``updates[update_idx]``;
  * ids stay the original subset ids, so a GT id is comparable with a returned
    node id without remapping.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from compute_gt import exact_gt, read_fvecs


def digest_bytes(*arrays: np.ndarray) -> str:
    hasher = hashlib.sha256()
    for array in arrays:
        hasher.update(np.ascontiguousarray(array).tobytes())
    return hasher.hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--n", required=True, type=int)
    parser.add_argument("--ids", type=Path, default=None)
    parser.add_argument("--updates", required=True, type=Path)
    parser.add_argument("--updates-manifest", required=True, type=Path)
    parser.add_argument("--op", required=True, choices=("erase", "upsert"))
    parser.add_argument("--counts", required=True,
                        help="cumulative update counts, e.g. 0,62,125,187,250")
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--chunk", type=int, default=25)
    args = parser.parse_args(argv)

    base = read_fvecs(args.base)[:args.n]
    updates = read_fvecs(args.updates)
    manifest = json.loads(args.updates_manifest.read_text())
    if manifest["n"] != args.n or manifest["count"] != len(updates):
        raise ValueError("update manifest does not match --n/--updates")
    if args.ids is not None:
        ids = np.load(args.ids).astype(np.uint32)
        if not np.array_equal(ids, np.arange(args.n, dtype=np.uint32)):
            raise ValueError("non-identity id mapping is not supported here")
    ids = np.arange(args.n, dtype=np.uint32)
    queries = read_fvecs(args.queries)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    counts = sorted({int(token) for token in args.counts.split(",")})
    if counts[-1] > len(updates):
        raise ValueError("checkpoint beyond the update log")
    vectors = base.astype(np.float32, copy=True)
    alive = np.ones(args.n, dtype=bool)
    applied = 0
    states = []
    for count in counts:
        while applied < count:
            entry = manifest["entries"][applied]
            node = int(entry["id"])
            if args.op == "erase":
                alive[node] = False
            else:
                vectors[node] = updates[applied]
            applied += 1
        active_ids = ids[alive]
        active_vectors = vectors[alive]
        started = time.monotonic()
        truth, meta = exact_gt(active_vectors, queries, args.k,
                               chunk=args.chunk)
        gt = active_ids[truth]
        path = args.out_dir / f"gt_{args.op}_c{count}_k{args.k}.u32"
        np.ascontiguousarray(gt, dtype="<u4").tofile(path)
        record = {
            "op": args.op, "cumulative_updates": count,
            "active_nodes": int(alive.sum()),
            "erased_nodes": int((~alive).sum()),
            "gt": str(path),
            "gt_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "state_sha256": digest_bytes(active_ids, active_vectors),
            "queries": int(len(queries)), "k": args.k,
            "gt_method": meta, "seconds": round(time.monotonic() - started, 1),
        }
        states.append(record)
        print(f"{args.op} c={count:6d} active={record['active_nodes']} "
              f"erased={record['erased_nodes']} gt={path.name} "
              f"({record['seconds']} s)", flush=True)

    (args.out_dir / f"checkpoint_states_{args.op}.json").write_text(
        json.dumps({"dataset": args.dataset, "n": args.n,
                    "updates": str(args.updates),
                    "updates_sha256": manifest["updates_sha256"],
                    "states": states}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
