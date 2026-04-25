#!/usr/bin/env python3
r"""Tier-1/2/3 bulk save benchmark for mesh-mem.

Environment variables:
    BENCH_N         Number of observations to save (default: 100)
    BENCH_PAYLOAD   Payload size in bytes per observation (default: 200)
    BENCH_WORKERS   Thread parallelism for save (default: 1)
    ZENOH_CONNECT   Zenoh router endpoint (default: tcp/127.0.0.1:7447)

Output: JSON to stdout with save throughput and search latency results.

Example (Tier-1):
    BENCH_N=100 BENCH_PAYLOAD=200 BENCH_WORKERS=1 \\
        PYTHONPATH=src python3 scripts/bench_bulk_save.py
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

os.environ.setdefault('ZENOH_CONNECT', 'tcp/127.0.0.1:7447')
os.environ.setdefault('MESH_MEM_AGENT_FAMILY', 'claude')
os.environ.setdefault('MESH_MEM_CLIENT_ID', 'bench-script')
# Fix session_id so all bench observations share one session label
os.environ.setdefault('MESH_MEM_SESSION_ID', f'bench-{int(time.time())}')

from mesh_mem import store  # noqa: E402
from mesh_mem.models import Observation  # noqa: E402


def _make_obs(i: int, payload_size: int) -> Observation:
    return Observation(
        content=f'bench-{i:06d} ' + 'A' * max(0, payload_size - 12),
        project='scale-bench',
        tags=['scale-test', 'tier1'],
    )


def bench_save(n: int, payload_size: int, workers: int = 1) -> dict:
    obs_list = [_make_obs(i, payload_size) for i in range(n)]
    start = time.perf_counter()
    if workers == 1:
        for obs in obs_list:
            store.put_observation(obs)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(store.put_observation, obs_list))
    elapsed = time.perf_counter() - start
    return {
        'n': n,
        'payload_b': payload_size,
        'workers': workers,
        'elapsed_s': round(elapsed, 3),
        'ops_per_sec': round(n / elapsed, 1),
    }


def bench_search(limits: list[int]) -> list[dict]:
    rows = []
    for lim in limits:
        start = time.perf_counter()
        hits = store.search_observations(project='scale-bench', limit=lim)
        elapsed = time.perf_counter() - start
        rows.append(
            {
                'limit': lim,
                'actual': len(hits),
                'latency_ms': round(elapsed * 1000, 1),
            }
        )
    return rows


if __name__ == '__main__':
    n = int(os.environ.get('BENCH_N', '100'))
    payload = int(os.environ.get('BENCH_PAYLOAD', '200'))
    workers = int(os.environ.get('BENCH_WORKERS', '1'))

    print(f'# BENCH_N={n} BENCH_PAYLOAD={payload} BENCH_WORKERS={workers}', flush=True)

    save_result = bench_save(n, payload, workers)
    search_results = bench_search([10, 50, 100, 500, 1000])

    output = {
        'save': save_result,
        'search': search_results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
