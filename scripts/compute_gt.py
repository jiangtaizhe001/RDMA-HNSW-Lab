#!/usr/bin/env python3
"""Compute exact L2 top-k ground truth for .fvecs inputs.

Output layout matches experiments/baseline1_main.cc `read_ground_truth`:
k little-endian uint32 node ids per query, row-major, no header.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np


def read_fvecs(path: Path) -> np.ndarray:
    if path.stat().st_size % 4:
        raise ValueError(f"truncated fvecs file: {path}")
    raw = np.fromfile(path, dtype="<i4")
    if raw.size == 0:
        raise ValueError(f"empty fvecs file: {path}")
    dim = int(raw[0])
    if dim <= 0:
        raise ValueError(f"invalid fvecs dimension: {path}")
    record = dim + 1
    if raw.size % record:
        raise ValueError(f"truncated fvecs file: {path}")
    records = raw.reshape(-1, record)
    if not np.all(records[:, 0] == dim):
        raise ValueError(f"inconsistent fvecs dimension: {path}")
    matrix = records[:, 1:].view("<f4").copy()
    if not np.isfinite(matrix).all():
        raise ValueError(f"nonfinite fvecs payload: {path}")
    return matrix


def exact_gt(base: np.ndarray, queries: np.ndarray, k: int = 10,
             chunk: int = 50) -> tuple[np.ndarray, dict]:
    if base.shape[1] != queries.shape[1] or not 0 < k <= len(base) or chunk <= 0:
        raise ValueError("invalid dimensions, k or chunk")
    base = base.astype(np.float64)
    queries = queries.astype(np.float64)
    base_sq = np.einsum("ij,ij->i", base, base)
    gt = np.empty((len(queries), k), dtype="<u4")
    ties = []
    for begin in range(0, len(queries), chunk):
        block = queries[begin:begin + chunk]
        distances = (base_sq[None, :] - 2.0 * block @ base.T +
                     np.einsum("ij,ij->i", block, block)[:, None])
        order = np.argsort(distances, axis=1, kind="stable")
        gt[begin:begin + len(block)] = order[:, :k]
        if k < len(base):
            for offset, row in enumerate(order):
                if distances[offset, row[k - 1]] == distances[offset, row[k]]:
                    ties.append(begin + offset)
    checked = np.linspace(0, len(queries) - 1, min(10, len(queries)), dtype=int)
    for query_id in checked:
        distances = np.sum((base - queries[query_id]) ** 2, axis=1)
        expected = np.argsort(distances, kind="stable")[:k]
        if not np.array_equal(expected, gt[query_id]):
            raise ValueError(f"independent GT check failed for query {query_id}")
    return gt, {"checked_queries": checked.tolist(), "boundary_ties": ties}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk", type=int, default=50)
    args = parser.parse_args()

    base = read_fvecs(args.base)
    queries = read_fvecs(args.queries)
    if base.shape[1] != queries.shape[1]:
        raise SystemExit("base/query dimension mismatch")
    if args.k <= 0 or args.k > base.shape[0]:
        raise SystemExit("invalid k")

    gt, checks = exact_gt(base, queries, args.k, args.chunk)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    gt.tofile(args.output)
    print(checks)
    print(f"wrote {gt.shape[0]} x {gt.shape[1]} uint32 GT -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
