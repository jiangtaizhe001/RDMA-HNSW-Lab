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
    raw = np.fromfile(path, dtype=np.int32)
    if raw.size == 0:
        raise SystemExit(f"empty fvecs file: {path}")
    dim = int(raw[0])
    record = dim + 1
    if raw.size % record:
        raise SystemExit(f"truncated fvecs file: {path}")
    matrix = raw.reshape(-1, record)[:, 1:].astype(np.float32)
    if matrix.shape[1] != dim:
        raise SystemExit(f"inconsistent fvecs dimension: {path}")
    return matrix


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

    base_sq = np.einsum("ij,ij->i", base, base)
    gt = np.empty((queries.shape[0], args.k), dtype="<u4")
    for begin in range(0, queries.shape[0], args.chunk):
        block = queries[begin:begin + args.chunk]
        d2 = (base_sq[None, :] - 2.0 * block @ base.T +
              np.einsum("ij,ij->i", block, block)[:, None])
        part = np.argpartition(d2, args.k - 1, axis=1)[:, :args.k]
        rows = np.arange(part.shape[0])[:, None]
        order = np.argsort(d2[rows, part], axis=1)
        gt[begin:begin + block.shape[0]] = part[rows, order]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    gt.tofile(args.output)
    print(f"wrote {gt.shape[0]} x {gt.shape[1]} uint32 GT -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
