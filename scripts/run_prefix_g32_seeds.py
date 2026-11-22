#!/usr/bin/env python3
"""Run the remaining construction seeds sequentially in a detached queue."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from run_prefix_g32_10k import OUTPUT, now, process_identity
from run_prefix_g32_efsearch import ROOT, digest, publish


def execute(queue):
    with (queue / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        publish(queue / "controller.json", dict(process_identity(__import__("os").getpid()),
                started_at=now(), command=[sys.executable, *sys.argv]))
        status = dict(state="running", seeds=[44, 45, 46], sizes=[20000, 50000],
                      completed_seeds=[], current_seed=None, campaigns={},
                      estimated_finish_at=None, timezone="UTC",
                      estimate_basis="Awaiting per-seed pilot calibration; queue ETA unavailable")
        process = None

        def interrupt(signum, frame):
            raise InterruptedError(f"signal {signum}")

        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        code, reason = 0, "all three seeds completed"
        try:
            for seed in status["seeds"]:
                campaign = OUTPUT / f"{queue.name}_seed{seed}"
                campaign.mkdir(exist_ok=True)
                status.update(current_seed=seed, updated_at=now())
                status["campaigns"][str(seed)] = str(campaign)
                publish(queue / "status.json", status)
                command = [sys.executable, str(ROOT / "scripts/run_prefix_g32_medium.py"),
                           "--campaign", str(campaign), "--sizes", "20000", "50000",
                           "--build-seed", str(seed)]
                with (campaign / "controller.log").open("a") as stream:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                               stdout=stream, stderr=stream)
                    publish(queue / f"seed{seed}_process.json",
                            dict(process_identity(process.pid), command=command, started_at=now()))
                    while True:
                        publish(queue / "heartbeat.json", dict(updated_at=now(), current_seed=seed,
                                monitor=process_identity(__import__("os").getpid())))
                        try:
                            result = process.wait(timeout=30)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                if result:
                    raise RuntimeError(f"seed {seed} failed with exit code {result}: {campaign}")
                if json.loads((campaign / "exit.json").read_text())["exit_code"] != 0:
                    raise RuntimeError(f"invalid exit evidence for seed {seed}")
                for size in status["sizes"]:
                    if not (campaign / f"N{size}_DONE.json").exists():
                        raise RuntimeError(f"missing completion evidence for seed {seed}, N={size}")
                status["completed_seeds"].append(seed)
            status.update(state="completed", current_seed=None, estimated_finish_at=now())
            publish(queue / "QUEUE_DONE.json", dict(exit_code=0, finished_at=now(),
                    campaigns=status["campaigns"]))
        except BaseException as error:
            code, reason = 1, repr(error)
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait()
            status.update(state="interrupted" if isinstance(error, InterruptedError) else "failed",
                          error=reason)
            raise
        finally:
            status["updated_at"] = now()
            publish(queue / "status.json", status)
            publish(queue / "exit.json", dict(exit_code=code, reason=reason, finished_at=now()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path)
    args = parser.parse_args()
    if args.queue:
        execute(args.queue.resolve())
        return
    queue = OUTPUT / ("seed_queue_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") +
                      "_" + digest(__file__)[:12])
    queue.mkdir(parents=True)
    if shutil.disk_usage(queue).free < 2 * 1024 ** 3:
        raise RuntimeError("less than 2 GiB free disk space")
    with (queue / "controller.log").open("a") as stream:
        process = subprocess.Popen(["nohup", sys.executable, str(Path(__file__).resolve()),
                                    "--queue", str(queue)], stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=stream, start_new_session=True)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        controller = json.loads((queue / "controller.json").read_text())
        if controller["pid"] != process.pid:
            raise RuntimeError("queue identity mismatch")
        print(json.dumps(dict(queue=str(queue), pid=process.pid, seeds=[44, 45, 46],
              sizes=[20000, 50000], status=str(queue / "status.json"),
              message="Background started; point and full queue ETA pending pilot calibration",
              timezone="UTC"), indent=2))
        return
    raise RuntimeError(f"queue exited during startup: {queue}")


if __name__ == "__main__":
    main()