#!/usr/bin/env python3
"""Prepare SIFT (or any .fvecs) datasets for the Baseline 1 experiments.

This implements task T7 of docs/baseline1-plan.md:

  * split the SIFT1M base set into fixed-size subsets (10K / 100K / 1M ...), and
  * generate *offline* L2 ground truth so that large indices can be evaluated
    without a brute-force ``--base`` scan inside the experiment driver
    (docs/baseline1-plan.md, pitfall 2).

The ``.fvecs`` format is a sequence of records: a little-endian int32 dimension
followed by that many little-endian float32 components.  It matches
``read_fvecs`` in src/common/data.cc, so files written here can be fed directly
to ``hnsw_build --input`` and ``baseline1_experiment --queries/--base``.

Ground truth is written as a raw little-endian int32 matrix of shape
(n_queries, k) -- exactly what ``baseline1_experiment --gt`` expects.

Usage
-----
    python3 scripts/convert_sift.py \
        --base  data/sift/sift_base.fvecs \
        --query data/sift/sift_query.fvecs \
        --out-dir data/sift --sizes 10000,100000,1000000 --k 10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np


def read_fvecs(path: Path) -> np.ndarray:
    """Read a .fvecs file into a contiguous float32 array of shape (n, dim)."""
    raw = np.fromfile(path, dtype=np.int32)
    if raw.size == 0:
        raise ValueError(f"empty .fvecs file: {path}")
    dim = int(raw[0])
    if dim <= 0:
        raise ValueError(f"invalid dimension {dim} in {path}")
    record = dim + 1
    if raw.size % record != 0:
        raise ValueError(
            f"malformed .fvecs file {path}: size {raw.size} not a multiple of "
            f"record size {record}")
    rows = raw.reshape(-1, record)
    # int32 and float32 share a width, so the payload can be reinterpreted.
    return np.ascontiguousarray(rows[:, 1:]).view(np.float32).copy()


def write_fvecs(path: Path, matrix: np.ndarray) -> None:
    """Write a float32 matrix to a .fvecs file (int32 dim prefix per row)."""
    matrix = np.ascontiguousarray(matrix, dtype=np.float32)
    n, dim = matrix.shape
    out = np.empty((n, dim + 1), dtype=np.int32)
    out[:, 0] = dim
    out[:, 1:] = matrix.view(np.int32)
    path.parent.mkdir(parents=True, exist_ok=True)
    out.tofile(path)


def ground_truth_l2(base: np.ndarray, queries: np.ndarray, k: int,
                    chunk: int = 256) -> np.ndarray:
    """Return the k nearest base ids for each query (exact L2, int32)."""
    base = np.ascontiguousarray(base, dtype=np.float32)
    queries = np.ascontiguousarray(queries, dtype=np.float32)
    if base.shape[1] != queries.shape[1]:
        raise ValueError("base and query dimensionality differ")
    k = min(k, base.shape[0])
    base_sq = np.einsum("ij,ij->i", base, base)
    gt = np.empty((queries.shape[0], k), dtype=np.int32)
    for start in range(0, queries.shape[0], chunk):
        block = queries[start:start + chunk]
        # Squared L2 = |b|^2 - 2 q.b + |q|^2 (the |q|^2 term is constant per row).
        d = (base_sq[None, :]
             - 2.0 * block @ base.T
             + np.einsum("ij,ij->i", block, block)[:, None])
        part = np.argpartition(d, k - 1, axis=1)[:, :k]
        rows = np.arange(block.shape[0])[:, None]
        order = np.argsort(d[rows, part], axis=1)
        gt[start:start + block.shape[0]] = np.take_along_axis(part, order, axis=1)
    return gt


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", required=True, type=Path,
                   help="SIFT base vectors (.fvecs)")
    p.add_argument("--query", type=Path, default=None,
                   help="SIFT query vectors (.fvecs); copied to the output dir")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--sizes", default="10000,100000",
                   help="comma separated subset sizes (default 10000,100000)")
    p.add_argument("--k", type=int, default=10, help="ground-truth depth")
    p.add_argument("--prefix", default="sift", help="output file name prefix")
    p.add_argument("--no-gt", action="store_true",
                   help="skip ground-truth generation")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    base = read_fvecs(args.base)
    print(f"base: {args.base} -> {base.shape[0]} vectors, dim {base.shape[1]}")

    queries = None
    if args.query is not None:
        queries = read_fvecs(args.query)
        print(f"query: {args.query} -> {queries.shape[0]} queries, "
              f"dim {queries.shape[1]}")
        write_fvecs(args.out_dir / f"{args.prefix}_query.fvecs", queries)

    sizes = []
    for token in args.sizes.split(","):
        token = token.strip()
        if token:
            sizes.append(min(int(token), base.shape[0]))

    for n in sizes:
        subset = base[:n]
        base_path = args.out_dir / f"{args.prefix}_base_{n}.fvecs"
        write_fvecs(base_path, subset)
        print(f"wrote {base_path} ({subset.shape[0]} vectors)")
        if queries is not None and not args.no_gt:
            gt = ground_truth_l2(subset, queries, args.k)
            gt_path = args.out_dir / f"{args.prefix}_gt_{n}_k{args.k}.bin"
            gt.tofile(gt_path)
            print(f"wrote {gt_path} ({gt.shape[0]}x{gt.shape[1]} int32 ids)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
