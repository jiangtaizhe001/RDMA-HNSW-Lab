#!/usr/bin/env python3
"""Local CM integration test on a real RDMA device; never changes network config."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='127.0.0.1', help='local RDMA/ERI IP or provider-supported loopback')
parser.add_argument('--device', default='')
parser.add_argument('--bind', default='0.0.0.0')
parser.add_argument('--port', default=18519, type=int)
parser.add_argument('--build', default='build')
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
build = (root / args.build).resolve()
with tempfile.TemporaryDirectory(prefix='rdma-hnsw-cm-') as temp:
    index = str(Path(temp) / 'index.bin')
    subprocess.run([str(build/'hnsw_build'), '--generate', '4096', '--dim', '32', '--output', index], check=True)
    log_path = Path(temp) / 'server.log'
    with log_path.open('w') as log:
        server = subprocess.Popen([str(build/'storage_server'), '--transport', 'rdma', '--rdma-connection', 'cm',
            '--bind', args.bind, '--port', str(args.port), '--ib-device', args.device, '--index', index], stdout=log, stderr=log)
        try:
            deadline = time.monotonic()+15
            while 'server listening' not in log_path.read_text():
                if server.poll() is not None:
                    raise RuntimeError('CM server exited: '+log_path.read_text())
                if time.monotonic() > deadline:
                    raise RuntimeError('CM listener startup timeout')
                time.sleep(.05)
            env = dict(os.environ, RDMA_CM_TEST_HOST=args.host, RDMA_CM_TEST_PORT=str(args.port), RDMA_CM_TEST_DEVICE=args.device)
            subprocess.run([str(build/'cm_integration_test')], env=env, check=True, timeout=120)
            if server.poll() is not None:
                raise RuntimeError('Server exited after client disconnect')
        finally:
            server.terminate()
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait()
            print(log_path.read_text())
