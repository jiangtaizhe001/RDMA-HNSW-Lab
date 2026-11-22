#!/usr/bin/env python3
"""Prepare real-vector datasets (SIFT1M / GIST1M) for the RDMA campaigns.

Implements sections 3 and 4 of `docs/real-data-rdma-improvement-plan.md`:

  * validate the archived dataset (dimension headers, finiteness, record count,
    byte order) instead of trusting the file name;
  * write prefix subsets ``base[0:N]`` for every requested scale, each with an
    explicit id map, and build one index per scale later;
  * split the official query set into disjoint calibration / quality / timing
    parts with a fixed permutation seed;
  * compute exact L2 top-k ground truth for every scale and every evaluation
    split, with a documented numeric path and an independent float64
    recomputation of at least ten queries.

The output directory receives ``.fvecs`` files (the format read by
``src/common/data.cc``), ``.u32`` ground-truth files (the format read by
``baseline1_experiment --gt``), per-file ``SHA256SUMS`` and a manifest that
records provenance, split sizes, GT method and all random seeds.

Truth files use ``uint32`` ids.  Distances are computed exactly: one float64
BLAS pass per query block against the whole base, a ``k+1`` partition per row
so the 10th/11th boundary can be tested, and ties broken by the smaller id.
An earlier float32 candidate + float64 re-rank variant was rejected because the
independent audit caught a rank inversion at 1M on SIFT: at million scale the
float32 approximation alone can push a true top-10 neighbour out of the
candidate set, so only the float64 path is used.

Usage
-----
    python3 scripts/prepare_real_dataset.py \
        --dataset sift --hdf5 data/real/downloads/sift-128-euclidean.hdf5 \
        --out-dir data/real/sift --sizes 10000,100000,1000000
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

CANDIDATE_MARGIN = 0
AUDIT_QUERIES = 10
DISTANCE_BLOCK_BYTES = 2.0e8


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 22), b""):
            sha.update(block)
    return sha.hexdigest()


def read_fvecs(path: Path) -> np.ndarray:
    raw = np.fromfile(path, dtype="<i4")
    if raw.size == 0:
        raise ValueError(f"empty fvecs file: {path}")
    dim = int(raw[0])
    if dim <= 0 or raw.size % (dim + 1):
        raise ValueError(f"malformed fvecs file: {path}")
    records = raw.reshape(-1, dim + 1)
    if not np.all(records[:, 0] == dim):
        raise ValueError(f"inconsistent fvecs dimension in {path}")
    matrix = np.ascontiguousarray(records[:, 1:]).view("<f4")
    if not np.all(np.isfinite(matrix)):
        raise ValueError(f"nonfinite fvecs payload in {path}")
    return matrix


def write_fvecs(path: Path, matrix: np.ndarray) -> None:
    matrix = np.ascontiguousarray(matrix, dtype=np.float32)
    records = np.empty((matrix.shape[0], matrix.shape[1] + 1), dtype="<i4")
    records[:, 0] = matrix.shape[1]
    records[:, 1:] = matrix.view("<i4")
    temporary = path.with_suffix(path.suffix + ".tmp")
    records.tofile(temporary)
    temporary.replace(path)


def load_hdf5(path: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    import h5py
    with h5py.File(path, "r") as handle:
        if "train" not in handle or "test" not in handle:
            raise ValueError(f"{path} lacks 'train'/'test' datasets")
        base = np.asarray(handle["train"], dtype=np.float32)
        queries = np.asarray(handle["test"], dtype=np.float32)
        attributes = {key: handle.attrs[key].decode() if isinstance(
            handle.attrs[key], bytes) else handle.attrs[key]
            for key in handle.attrs}
        neighbours = ("neighbors" in handle,
                      tuple(handle["neighbors"].shape) if "neighbors" in handle else None)
    meta = {"base_shape": list(base.shape), "query_shape": list(queries.shape),
            "attributes": attributes, "has_official_neighbors": neighbours[0],
            "official_neighbors_shape": neighbours[1]}
    return base, queries, meta


def validate(base: np.ndarray, queries: np.ndarray) -> dict:
    if base.ndim != 2 or queries.ndim != 2:
        raise ValueError("base/queries must be matrices")
    if base.shape[1] != queries.shape[1]:
        raise ValueError("base/query dimension mismatch")
    if not np.all(np.isfinite(base)) or not np.all(np.isfinite(queries)):
        raise ValueError("nonfinite vector payload")
    if base.dtype != np.float32 or queries.dtype != np.float32:
        raise ValueError("payload is not float32")
    return {"base_records": int(base.shape[0]), "query_records": int(queries.shape[0]),
            "dim": int(base.shape[1]),
            "base_bytes": int(base.nbytes), "query_bytes": int(queries.nbytes),
            "base_min": float(base.min()), "base_max": float(base.max()),
            "query_min": float(queries.min()), "query_max": float(queries.max())}


def exact_topk(base: np.ndarray, queries: np.ndarray, k: int) -> tuple[np.ndarray, list[int], dict]:
    """Exact L2 top-k in float64, ties broken by the smaller id."""
    n = base.shape[0]
    if k >= n:
        raise ValueError("k must be smaller than the base size")
    base64 = np.ascontiguousarray(base, dtype=np.float64)
    base_sq = np.einsum("ij,ij->i", base64, base64)
    block = max(1, int(DISTANCE_BLOCK_BYTES // (8 * n)))
    gt = np.empty((len(queries), k), dtype=np.int64)
    ties: list[int] = []
    for start in range(0, len(queries), block):
        chunk = queries[start:start + block].astype(np.float64)
        distances = (base_sq[None, :] - 2.0 * (chunk @ base64.T)
                     + np.einsum("ij,ij->i", chunk, chunk)[:, None])
        part = np.argpartition(distances, k, axis=1)[:, :k + 1]
        for row in range(chunk.shape[0]):
            candidates = part[row]
            exact = distances[row, candidates]
            order = np.lexsort((candidates, exact))
            gt[start + row] = candidates[order[:k]]
            if exact[order[k - 1]] == exact[order[k]]:
                ties.append(start + row)
        del distances, part
    return gt, ties, {"candidate_width": int(k + 1), "query_block": int(block),
                      "precision": "float64 BLAS", "tie_break": "smaller id"}


def brute_force_topk(base: np.ndarray, query: np.ndarray, k: int,
                     block: int = 32768) -> tuple[np.ndarray, np.ndarray]:
    """Independent float64 audit: direct squared differences over all rows."""
    best_ids = np.empty(0, dtype=np.int64)
    best_dist = np.empty(0, dtype=np.float64)
    target = query.astype(np.float64)
    for start in range(0, base.shape[0], block):
        rows = base[start:start + block].astype(np.float64) - target
        distances = np.einsum("ij,ij->i", rows, rows)
        ids = np.arange(start, start + len(distances), dtype=np.int64)
        merged_dist = np.concatenate([best_dist, distances])
        merged_ids = np.concatenate([best_ids, ids])
        order = np.argsort(merged_dist, kind="stable")[:k]
        best_dist = merged_dist[order]
        best_ids = merged_ids[order]
    return best_ids, best_dist


def split_queries(count: int, calibration: int, quality: int,
                  seed: int) -> tuple[dict[str, np.ndarray], dict]:
    if count < 3:
        raise ValueError("need at least three queries")
    requested = calibration + quality
    if requested >= count:
        calibration = max(1, count // 4)
        quality = max(1, count // 4)
    order = np.random.default_rng(seed).permutation(count)
    splits = {"calibration": order[:calibration],
              "quality": order[calibration:calibration + quality],
              "timing": order[calibration + quality:]}
    return splits, {"requested_calibration": requested,
                    "actual_calibration": int(len(splits["calibration"])),
                    "actual_quality": int(len(splits["quality"])),
                    "actual_timing": int(len(splits["timing"])),
                    "reduced_from_request": bool(calibration + quality < requested)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--hdf5", type=Path, default=None)
    parser.add_argument("--base-fvecs", type=Path, default=None)
    parser.add_argument("--query-fvecs", type=Path, default=None)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--sizes", default="10000,100000,1000000")
    parser.add_argument("--calibration", type=int, default=1000)
    parser.add_argument("--quality", type=int, default=1000)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42,
                        help="seed of the query permutation")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    started = time.monotonic()
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    sizes = sorted({int(token) for token in args.sizes.split(",") if token.strip()})

    sources: dict[str, str] = {}
    if args.hdf5 is not None:
        base, queries, source_meta = load_hdf5(args.hdf5)
        sources["hdf5"] = str(args.hdf5)
        sources["hdf5_sha256"] = digest(args.hdf5)
        downlink = args.hdf5.with_name(args.hdf5.name + ".manifest.json")
        if downlink.exists():
            sources["download_manifest"] = json.loads(downlink.read_text())
    elif args.base_fvecs and args.query_fvecs:
        base = read_fvecs(args.base_fvecs)
        queries = read_fvecs(args.query_fvecs)
        source_meta = {}
        sources["base_fvecs"] = str(args.base_fvecs)
        sources["query_fvecs"] = str(args.query_fvecs)
        sources["base_sha256"] = digest(args.base_fvecs)
        sources["query_sha256"] = digest(args.query_fvecs)
    else:
        raise SystemExit("give --hdf5 or both --base-fvecs/--query-fvecs")

    facts = validate(base, queries)
    nq = queries.shape[0]
    if base.shape[0] < max(sizes):
        raise SystemExit(f"base holds {base.shape[0]} records, need {max(sizes)}")
    if nq < 3:
        raise SystemExit(f"query set holds only {nq} records")

    splits, split_meta = split_queries(nq, args.calibration, args.quality, args.seed)
    for name, indices in splits.items():
        path = out / f"{args.dataset}_query_{name}.fvecs"
        write_fvecs(path, queries[indices])
        np.save(out / f"{args.dataset}_query_{name}.idx.npy", indices)
        print(f"queries {name}: {len(indices)} -> {path}")

    products: list[Path] = []
    gt_meta: dict[str, dict] = {}
    for size in sizes:
        subset = base[:size]
        base_path = out / f"{args.dataset}_base_n{size}.fvecs"
        if args.force or not base_path.exists():
            write_fvecs(base_path, subset)
        np.save(out / f"{args.dataset}_base_n{size}.ids.npy",
                np.arange(size, dtype=np.int64))
        products.append(base_path)
        reread = read_fvecs(base_path)
        if not np.array_equal(reread, subset):
            raise ValueError(f"round-trip mismatch for {base_path}")
        del reread
        print(f"subset n={size}: {base_path} "
              f"({base_path.stat().st_size / 1e9:.2f} GB)")

        for split in ("calibration", "quality"):
            indices = splits[split]
            begin = time.monotonic()
            truth, ties, method = exact_topk(subset, queries[indices], args.k)
            gt_path = out / f"{args.dataset}_gt_n{size}_{split}_k{args.k}.u32"
            truth.astype("<u4").tofile(gt_path)
            products.append(gt_path)
            audit_rows = np.linspace(0, len(indices) - 1,
                                     min(AUDIT_QUERIES, len(indices)), dtype=int)
            audit = {"checked_queries": [int(indices[row]) for row in audit_rows],
                     "mismatched": [], "boundary_equal_distance": []}
            for row in audit_rows:
                ids, distances = brute_force_topk(subset, queries[indices[row]],
                                                 args.k)
                if not np.array_equal(ids, truth[row]):
                    audit["mismatched"].append(int(indices[row]))
                    if set(ids.tolist()) != set(truth[row].tolist()):
                        raise ValueError(
                            f"audit mismatch for {args.dataset} n={size} "
                            f"{split} query {indices[row]}")
                boundary = brute_force_topk(subset, queries[indices[row]],
                                            args.k + 1)[1]
                if boundary[args.k - 1] == boundary[args.k]:
                    audit["boundary_equal_distance"].append(int(indices[row]))
            if audit["mismatched"]:
                # Same set, different order: legitimate tie ordering.
                print(f"  note: {len(audit['mismatched'])} audit rows differ in "
                      f"order only (ties) for n={size} {split}")
            gt_meta[f"n{size}_{split}"] = {
                "path": str(gt_path), "sha256": digest(gt_path),
                "queries": int(len(indices)), "k": args.k,
                "method": "exact float64 BLAS, top-(k+1) partition, ties by id",
                "method_detail": method, "boundary_tie_queries": len(ties),
                "audit": audit,
                "seconds": round(time.monotonic() - begin, 2)}
            print(f"  GT {split}: {gt_path} ({gt_meta[f'n{size}_{split}']['seconds']} s, "
                  f"{len(ties)} boundary ties, audit rows {len(audit_rows)})")
        del subset

    sums = out / "SHA256SUMS"
    lines = []
    for path in sorted(products) + sorted(out.glob(f"{args.dataset}_query_*.fvecs")) \
            + sorted(out.glob(f"{args.dataset}_query_*.idx.npy")):
        lines.append(f"{digest(path)}  {path.name}")
    sums.write_text("\n".join(lines) + "\n")

    manifest = {
        "dataset": args.dataset, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "sources": sources, "source_meta": source_meta, "facts": facts,
        "sizes": sizes, "subset_rule": "prefix base[0:N] with identity id map",
        "query_split": dict(split_meta, seed=args.seed,
                            permutation="np.random.default_rng(seed).permutation"),
        "query_split_indices": {name: {"file": f"{args.dataset}_query_{name}.idx.npy",
                                       "count": int(len(indices))}
                                for name, indices in splits.items()},
        "ground_truth": gt_meta, "k": args.k,
        "candidate_margin": CANDIDATE_MARGIN,        "elapsed_seconds": round(time.monotonic() - started, 2),
        "python": sys.version.split()[0], "numpy": np.__version__,
        "sha256sums": str(sums),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    card = [f"# {args.dataset} dataset card", "",
            f"- dim: {facts['dim']}, records: {facts['base_records']}, "
            f"queries: {facts['query_records']}",
            f"- source: {sources.get('download_manifest', {}).get('url', sources)}",
            f"- archive SHA-256: "
            f"{sources.get('download_manifest', {}).get('sha256', sources.get('hdf5_sha256'))}",
            f"- subset rule: {manifest['subset_rule']}",
            f"- query split seed {args.seed}: "
            f"calibration {split_meta['actual_calibration']}, "
            f"quality {split_meta['actual_quality']}, "
            f"timing {split_meta['actual_timing']}",
            f"- GT: exact L2 top-{args.k} in float64 (whole base per query block, "
            f"top-(k+1) partition, ties by smaller id); "
            f"{AUDIT_QUERIES}-query float64 brute-force audit per split",
            "", "| scale | file | bytes |", "| --- | --- | --- |"]
    for size in sizes:
        path = out / f"{args.dataset}_base_n{size}.fvecs"
        card.append(f"| {size} | `{path.name}` | {path.stat().st_size} |")
    (out / "data_card.md").write_text("\n".join(card) + "\n")
    print(f"done in {manifest['elapsed_seconds']} s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
