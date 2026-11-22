#!/usr/bin/env python3
"""Resumable multi-connection downloader for the real data campaigns.

The upstream mirrors used by `docs/real-data-rdma-improvement-plan.md`
(`ann-benchmarks.com`, the texmex FTP host) throttle single TCP streams to a
few tens of KB/s from this site, so a plain `curl` of the 525 MB SIFT archive
or the 3.8 GB GIST archive is not practical.  The server does honour HTTP
`Range` requests, so this tool splits the object into fixed-size chunks, pulls
them over several connections, and only publishes the final file after every
chunk is complete and the byte count matches `Content-Length`.

Nothing here is specific to a dataset: the source URL, expected size and the
resulting SHA-256 are recorded next to the artefact so the preparation step can
state provenance instead of silently trusting a file name.

Usage
-----
    python3 scripts/download_real_dataset.py \
        --url https://ann-benchmarks.com/sift-128-euclidean.hdf5 \
        --out data/real/downloads/sift-128-euclidean.hdf5

Interrupted runs resume: completed chunks stay in `<out>.parts/` and are reused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.request


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) RDMA-HNSW-Lab-dataset-fetch/1.0"


def head(url: str) -> dict:
    request = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return {key.lower(): value for key, value in response.headers.items()}


def fetch_range(url: str, start: int, end: int, path: Path) -> None:
    """Download the inclusive byte range [start, end] into `path`."""
    expected = end - start + 1
    if path.exists() and path.stat().st_size == expected:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    request = urllib.request.Request(
        url, headers={"Range": f"bytes={start}-{end}", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, \
            temporary.open("wb") as stream:
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            stream.write(block)
    if temporary.stat().st_size != expected:
        temporary.unlink()
        raise RuntimeError(f"short range {start}-{end}: "
                           f"{temporary.stat().st_size} != {expected}")
    temporary.replace(path)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--connections", type=int, default=16)
    parser.add_argument("--chunk-mb", type=int, default=4)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--attempts", type=int, default=400,
                        help="maximum number of chunk attempts before giving up")
    args = parser.parse_args(argv)

    headers = head(args.url)
    total = int(headers.get("content-length", "0"))
    if not total:
        raise SystemExit("server did not report Content-Length")
    if headers.get("accept-ranges", "").lower() != "bytes":
        raise SystemExit("server does not advertise Range support")

    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    parts = out.parent / (out.name + ".parts")
    parts.mkdir(exist_ok=True)

    chunk = args.chunk_mb << 20
    ranges = [(start, min(start + chunk, total) - 1)
              for start in range(0, total, chunk)]
    pending = [(start, end) for start, end in ranges
               if not (parts / f"{start:012d}-{end:012d}").exists()
               or (parts / f"{start:012d}-{end:012d}").stat().st_size
               != end - start + 1]
    print(f"{args.url}: {total} bytes in {len(ranges)} chunks, "
          f"{len(pending)} pending", flush=True)

    lock = threading.Lock()
    queue = list(pending)
    failures: list[str] = []
    done = [len(ranges) - len(pending)]
    started = time.monotonic()

    def worker() -> None:
        while True:
            with lock:
                if not queue:
                    return
                start, end = queue.pop(0)
            path = parts / f"{start:012d}-{end:012d}"
            for attempt in range(1, args.retries + 1):
                try:
                    fetch_range(args.url, start, end, path)
                    break
                except (urllib.error.URLError, OSError, RuntimeError) as error:
                    if attempt == args.retries:
                        with lock:
                            failures.append(f"{start}-{end}: {error}")
                        return
                    time.sleep(min(30.0, 2.0 ** attempt * 0.5))
            with lock:
                done[0] += 1
                if done[0] % 16 == 0 or done[0] == len(ranges):
                    rate = (done[0] * chunk) / max(1e-9, time.monotonic() - started)
                    print(f"  {done[0]}/{len(ranges)} chunks "
                          f"({rate / 1e6:.2f} MB/s)", flush=True)

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(args.connections)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if failures:
        print("incomplete chunks:", *failures, sep="\n  ", file=sys.stderr)
        return 1

    temporary = out.with_suffix(out.suffix + ".assembling")
    with temporary.open("wb") as stream:
        for start, end in ranges:
            with (parts / f"{start:012d}-{end:012d}").open("rb") as part:
                while True:
                    block = part.read(1 << 22)
                    if not block:
                        break
                    stream.write(block)
    if temporary.stat().st_size != total:
        raise SystemExit(f"assembled size {temporary.stat().st_size} != {total}")
    temporary.replace(out)

    digest = hashlib.sha256()
    with out.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 22), b""):
            digest.update(block)
    manifest = {"url": args.url, "path": str(out), "bytes": total,
                "sha256": digest.hexdigest(),
                "last_modified": headers.get("last-modified"),
                "connections": args.connections, "chunk_bytes": chunk,
                "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    out.with_name(out.name + ".manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    for start, end in ranges:
        (parts / f"{start:012d}-{end:012d}").unlink()
    parts.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
