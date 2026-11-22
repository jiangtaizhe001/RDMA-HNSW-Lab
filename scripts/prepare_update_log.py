#!/usr/bin/env python3
"""Build the shared, deterministic update log for the mixed (P2) matrix.

Section 7 of `docs/real-data-rdma-improvement-plan.md` requires that every
strategy of a comparison unit sees the *same* update sequence, that consecutive
erases never delete the same id twice, and that the applied vectors are fixed
before the run instead of being drawn per strategy.

The driver already picks ids deterministically:

  id(ui) = ui % node_count            (roundrobin, the default distribution)

so the only thing that has to be pinned down outside the binary is the vector
written by an upsert.  This script

  * writes ``<dataset>_updates_n<n>_k<count>.fvecs`` with one real base vector
    per update (``base[(ui + n/2) % n]``), i.e. a vector from the same
    distribution that is never the vector of the node it replaces;
  * writes a manifest with the id/vector rules, the SHA-256 of the inputs and
    the per-entry ``(id, source_row, vector_sha256)`` audit trail;
  * optionally writes cumulative prefixes ``..._first<c>.fvecs`` used by the
    checkpoint replay (the driver applies exactly as many updates as the file
    holds, so a prefix replays the first ``c`` updates from scratch).

The log is data, not code: the campaign passes the same file to every strategy
and records its SHA-256 in the run identity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


def read_fvecs(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype="<i4")
    if raw.size == 0:
        raise ValueError(f"empty fvecs file: {path}")
    dim = int(raw[0])
    record = dim + 1
    if dim <= 0 or raw.size % record:
        raise ValueError(f"malformed fvecs file: {path}")
    records = raw.reshape(-1, record)
    if not np.all(records[:, 0] == dim):
        raise ValueError(f"inconsistent fvecs dimension: {path}")
    return records[:, 1:].view("<f4").copy()


def write_fvecs(path: Path, matrix: np.ndarray) -> None:
    records = np.empty((len(matrix), matrix.shape[1] + 1), dtype="<i4")
    records[:, 0] = matrix.shape[1]
    records[:, 1:] = matrix.view("<i4")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".fvecs.tmp")
    records.tofile(temporary)
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--n", required=True, type=int)
    parser.add_argument("--count", required=True, type=int)
    parser.add_argument("--slices", default="",
                        help="cumulative prefixes, e.g. 62,125,187,250")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    base = read_fvecs(args.base)
    if len(base) < args.n:
        raise ValueError(f"base has {len(base)} rows, need {args.n}")
    if not 0 < args.count <= args.n:
        raise ValueError("count must be within 1..n so roundrobin ids never repeat")
    base = base[:args.n]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    sources = (np.arange(args.count) + args.n // 2) % args.n
    ids = np.arange(args.count) % args.n
    if np.any(sources == ids):
        raise ValueError("replacement vector would equal the replaced node")
    updates = np.ascontiguousarray(base[sources])

    stem = f"{args.dataset}_updates_n{args.n}_k{args.count}"
    out = args.out_dir / f"{stem}.fvecs"
    write_fvecs(out, updates)

    entries = [{"update_idx": int(ui), "id": int(ids[ui]),
                "source_row": int(sources[ui]),
                "vector_sha256": hashlib.sha256(
                    updates[ui].tobytes()).hexdigest()[:16]}
               for ui in range(args.count)]
    slices = []
    for token in [part.strip() for part in args.slices.split(",") if part.strip()]:
        prefix = int(token)
        if not 0 <= prefix <= args.count:
            raise ValueError(f"slice {prefix} outside 0..{args.count}")
        path = args.out_dir / f"{stem}_first{prefix}.fvecs"
        write_fvecs(path, np.ascontiguousarray(updates[:prefix]))
        slices.append({"count": prefix, "path": str(path),
                       "sha256": sha256(path)})

    manifest = {
        "dataset": args.dataset, "n": args.n, "count": args.count,
        "dim": int(base.shape[1]),
        "id_rule": "roundrobin id = update_idx % n (driver default)",
        "vector_rule": "base[(update_idx + n//2) % n] (real base vector)",
        "base": str(args.base), "base_sha256": sha256(args.base),
        "updates": str(out), "updates_sha256": sha256(out),
        "unique_ids": int(len(set(int(value) for value in ids))),
        "erase_repeats": 0 if len(set(int(v) for v in ids)) == args.count else
        args.count - len(set(int(value) for value in ids)),
        "slices": slices, "entries": entries,
    }
    (args.out_dir / f"{stem}.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out} ({args.count} vectors, dim {base.shape[1]}) "
          f"and {args.out_dir / f'{stem}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
