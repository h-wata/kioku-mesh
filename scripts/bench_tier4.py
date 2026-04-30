#!/usr/bin/env python3
"""Tier-4 benchmark: end-to-end search_observations at 50k obs via full zenoh path.

Phase 5 of TASK-131 (Issue #7). Validates sub-200ms@50k acceptance criterion
for the SQLite-first search path implemented in Phases 1-4.

Usage:
    cd /home/gisen/work/mesh-mem
    PYTHONPATH=src python3 scripts/bench_tier4.py

Environment:
    ZENOH_CONNECT           Router endpoint (default: tcp/127.0.0.1:7447)
    BENCH_STATE_DIR         SQLite state dir (default: /tmp/bench_tier4)
    BENCH_ITERATIONS        Search iterations per size (default: 100)
    BENCH_FALLBACK_ITERS    Iterations for zenoh fallback section (default: 5)
    BENCH_SKIP_FALLBACK     Skip zenoh fallback section when 1 (default: 0)
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import sys
import time

# ---- env BEFORE mesh_mem import ----
_BENCH_STATE_DIR = os.environ.get('BENCH_STATE_DIR', '/tmp/bench_tier4')
os.environ['MESH_MEM_STATE_DIR'] = _BENCH_STATE_DIR
os.environ['MESH_MEM_INDEX_DB'] = str(Path(_BENCH_STATE_DIR) / 'index.db')
os.environ.setdefault('ZENOH_CONNECT', 'tcp/127.0.0.1:7447')
os.environ.setdefault('MESH_MEM_AGENT_FAMILY', 'claude')
os.environ.setdefault('MESH_MEM_CLIENT_ID', 'bench-tier4')
os.environ.setdefault('MESH_MEM_SESSION_ID', f'bench-tier4-{int(time.time())}')
os.environ['MESH_MEM_SKIP_REBUILD'] = '1'  # we control data; skip auto-rebuild

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

import yaml  # noqa: E402

from mesh_mem.local_index import LocalIndex  # noqa: E402
from mesh_mem.models import Observation  # noqa: E402
import mesh_mem.store as _store  # noqa: E402

SIZES = [100, 1_000, 6_000, 16_000, 50_000]
PROJECT = 'bench-tier4'
ITERATIONS = int(os.environ.get('BENCH_ITERATIONS', '100'))
FALLBACK_ITERS = int(os.environ.get('BENCH_FALLBACK_ITERS', '5'))
SKIP_FALLBACK = os.environ.get('BENCH_SKIP_FALLBACK', '0') == '1'


def _make_obs(i: int) -> Observation:
    return Observation(
        content=f'bench-tier4-{i:06d} ' + 'X' * 180,
        project=PROJECT,
        tags=[f'tag-{i % 17}', 'bench-tier4'],
        memory_type='note' if i % 3 else 'fact',
        importance=(i % 5) + 1,
        subject=f'subject-{i % 137}',
        summary=f'summary for observation {i}',
    )


def _p99(samples_sorted: list[float]) -> float:
    idx = max(0, int(len(samples_sorted) * 0.99) - 1)
    return samples_sorted[idx]


def _measure_search(limit: int, n_iters: int) -> dict:
    samples: list[float] = []
    actual = 0
    for _ in range(n_iters):
        t0 = time.perf_counter()
        results = _store.search_observations(project=PROJECT, limit=limit)
        samples.append((time.perf_counter() - t0) * 1000.0)
        actual = len(results)
    samples.sort()
    return {
        'p50_ms': round(statistics.median(samples), 3),
        'p99_ms': round(_p99(samples), 3),
        'min_ms': round(samples[0], 3),
        'max_ms': round(samples[-1], 3),
        'actual_results': actual,
        'iterations': n_iters,
    }


def _measure_find_by_id(obs_id: str, n_iters: int) -> dict:
    samples: list[float] = []
    for _ in range(n_iters):
        t0 = time.perf_counter()
        _store.find_observation_by_id(obs_id)
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    return {
        'p50_ms': round(statistics.median(samples), 3),
        'p99_ms': round(_p99(samples), 3),
        'iterations': n_iters,
    }


def run_put_phase() -> tuple[dict[int, str], list[float]]:
    """Put 50k obs incrementally; return {size: first_obs_id} and put times."""
    print('\n=== Phase A: put 50k observations via zenoh ===', flush=True)

    state_path = Path(_BENCH_STATE_DIR)
    if state_path.exists():
        shutil.rmtree(state_path)
    state_path.mkdir(parents=True, exist_ok=True)
    _store._reset_index()  # noqa: SLF001
    _store._reset_session()  # noqa: SLF001

    checkpoint_ids: dict[int, str] = {}
    put_times: list[float] = []
    total = 0

    for size in SIZES:
        delta = size - total
        first_id: str | None = None
        t0 = time.perf_counter()
        for i in range(total, size):
            obs = _make_obs(i)
            if first_id is None:
                first_id = obs.observation_id
            _store.put_observation(obs)
        elapsed = time.perf_counter() - t0
        total = size
        rate = delta / elapsed if elapsed > 0 else 0
        checkpoint_ids[size] = first_id  # type: ignore[assignment]
        put_times.append(elapsed)
        print(f'  → {size:>6,}: +{delta:>6,} in {elapsed:6.1f}s ({rate:7.0f} obs/s)', flush=True)

    return checkpoint_ids, put_times


def run_sqlite_first_bench(checkpoint_ids: dict[int, str]) -> dict:
    """Measure at each size checkpoint (SQLite-first, n=ITERATIONS each)."""
    print(f'\n=== Phase B: SQLite-first latency (n={ITERATIONS}) ===', flush=True)

    os.environ.pop('MESH_MEM_DISABLE_INDEX', None)
    _store._reset_index()  # noqa: SLF001

    results: dict[str, dict] = {}
    for size in SIZES:
        obs_id = checkpoint_ids[size]
        print(f'\n  -- size={size:,} --', flush=True)
        s50 = _measure_search(50, ITERATIONS)
        s1000 = _measure_search(1000, ITERATIONS)
        fid = _measure_find_by_id(obs_id, ITERATIONS)
        print(
            f'     limit=50    p50={s50["p50_ms"]:7.2f}ms  p99={s50["p99_ms"]:7.2f}ms',
            flush=True,
        )
        print(
            f'     limit=1000  p50={s1000["p50_ms"]:7.2f}ms  p99={s1000["p99_ms"]:7.2f}ms',
            flush=True,
        )
        print(
            f'     find_by_id  p50={fid["p50_ms"]:7.2f}ms  p99={fid["p99_ms"]:7.2f}ms',
            flush=True,
        )
        results[f'size_{size}'] = {
            'search_limit_50_p50_ms': s50['p50_ms'],
            'search_limit_50_p99_ms': s50['p99_ms'],
            'search_limit_1000_p50_ms': s1000['p50_ms'],
            'search_limit_1000_p99_ms': s1000['p99_ms'],
            'find_by_id_p50_ms': fid['p50_ms'],
            'find_by_id_p99_ms': fid['p99_ms'],
            'detail': {
                'search_limit_50': s50,
                'search_limit_1000': s1000,
                'find_by_id': fid,
            },
        }
    return results


def run_rebuild_bench() -> dict:
    """Delete SQLite and time rebuild_from_zenoh against the 50k zenoh store."""
    print('\n=== Phase C: rebuild_from_zenoh at 50k ===', flush=True)

    _store._reset_index()  # noqa: SLF001
    db_path = Path(_BENCH_STATE_DIR) / 'index.db'
    for suffix in ('', '-wal', '-shm'):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    print('  SQLite deleted; rebuilding from zenoh...', flush=True)

    idx = LocalIndex.connect(str(db_path))
    session = _store.get_session()

    t0 = time.perf_counter()
    stats = idx.rebuild_from_zenoh(session)
    elapsed = time.perf_counter() - t0

    print(
        f'  rebuilt {stats.added:,} rows in {elapsed:.2f}s ({stats.added / elapsed:.0f} rows/s)',
        flush=True,
    )
    idx.close()

    return {
        'duration_sec': round(elapsed, 3),
        'rows_added': stats.added,
        'rows_per_sec': round(stats.added / elapsed, 1) if elapsed > 0 else 0,
        'passed_30s_target': elapsed < 30.0,
    }


def run_subscriber_lag(n_samples: int = 10) -> dict:
    """Raw zenoh put → subscriber callback → SQLite upsert round-trip time."""
    print(f'\n=== Phase D: subscriber lag (n={n_samples}) ===', flush=True)

    os.environ.pop('MESH_MEM_DISABLE_INDEX', None)
    os.environ['MESH_MEM_SKIP_REBUILD'] = '1'
    _store._reset_index()  # noqa: SLF001

    idx = _store.get_index()
    session = _store.get_session()

    lags_ms: list[float] = []
    for i in range(n_samples):
        obs = _make_obs(100_000 + i)
        before = idx.row_count()
        t0 = time.perf_counter()
        # raw put — subscriber callback path only (no direct idx.upsert)
        session.put(obs.key_expr, obs.to_json())
        deadline = t0 + 3.0
        while time.perf_counter() < deadline:
            if idx.row_count() > before:
                lags_ms.append((time.perf_counter() - t0) * 1000.0)
                break
            time.sleep(0.001)
        else:
            print(f'  WARNING: sample {i} not captured within 3s', flush=True)

    if not lags_ms:
        result: dict = {'note': 'no samples captured', 'p50_ms': None, 'max_ms': None}
    else:
        lags_ms.sort()
        result = {
            'p50_ms': round(statistics.median(lags_ms), 3),
            'max_ms': round(max(lags_ms), 3),
            'min_ms': round(min(lags_ms), 3),
            'n': len(lags_ms),
        }
        print(
            f'  subscriber lag  p50={result["p50_ms"]:.1f}ms  max={result["max_ms"]:.1f}ms',
            flush=True,
        )
    return result


def run_fallback_bench() -> dict:
    """Zenoh full-scan at 50k (MESH_MEM_DISABLE_INDEX=1, n=FALLBACK_ITERS).

    The default GET_TIMEOUT (5s) is too short for a 50k full-scan; we raise it
    to 60s for this section only, restoring the original value on exit.
    If the scan still times out, the result records the bound rather than failing.
    """
    if SKIP_FALLBACK:
        print('\n=== Phase E: zenoh fallback SKIPPED ===', flush=True)
        return {'skipped': True}

    print(f'\n=== Phase E: zenoh fallback at 50k (n={FALLBACK_ITERS}) ===', flush=True)
    print('  NOTE: full zenoh scan — expect multi-second latency', flush=True)

    original_timeout = _store.GET_TIMEOUT  # noqa: SLF001
    _store.GET_TIMEOUT = 60.0  # noqa: SLF001
    os.environ['MESH_MEM_DISABLE_INDEX'] = '1'
    _store._reset_index()  # noqa: SLF001

    try:
        s50 = _measure_search(50, FALLBACK_ITERS)
        s1000 = _measure_search(1000, FALLBACK_ITERS)
        print(
            f'  limit=50    p50={s50["p50_ms"]:,.1f}ms  p99={s50["p99_ms"]:,.1f}ms',
            flush=True,
        )
        print(
            f'  limit=1000  p50={s1000["p50_ms"]:,.1f}ms  p99={s1000["p99_ms"]:,.1f}ms',
            flush=True,
        )
        return {
            'size_50000': {
                'search_limit_50_p50_ms': s50['p50_ms'],
                'search_limit_50_p99_ms': s50['p99_ms'],
                'search_limit_1000_p50_ms': s1000['p50_ms'],
                'search_limit_1000_p99_ms': s1000['p99_ms'],
                'detail': {'search_limit_50': s50, 'search_limit_1000': s1000},
            },
        }
    except (RuntimeError, Exception) as exc:
        print(f'  fallback measurement failed: {exc}', flush=True)
        bound_ms = original_timeout * 1000
        return {
            'note': f'scan timed out: {exc}',
            'size_50000': {
                'search_limit_50_p50_ms': f'>{bound_ms:.0f}ms (GET_TIMEOUT)',
                'search_limit_1000_p50_ms': f'>{bound_ms:.0f}ms (GET_TIMEOUT)',
            },
        }
    finally:
        _store.GET_TIMEOUT = original_timeout  # noqa: SLF001
        os.environ.pop('MESH_MEM_DISABLE_INDEX', None)
        _store._reset_index()  # noqa: SLF001


def _db_size_mb(db_path: Path) -> float:
    total = 0
    for suffix in ('', '-wal', '-shm'):
        p = Path(str(db_path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return round(total / (1024 * 1024), 2)


def main() -> int:
    measured_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+09:00')
    print(f'bench_tier4 start: {measured_at}', flush=True)

    # Phase A: incremental put
    checkpoint_ids, put_times = run_put_phase()

    # Phase B: SQLite-first
    sqlite_results = run_sqlite_first_bench(checkpoint_ids)

    # Phase C: rebuild
    rebuild = run_rebuild_bench()

    # Phase D: subscriber lag
    sub_lag = run_subscriber_lag(10)

    # Phase E: fallback
    fallback = run_fallback_bench()

    # DB sizes
    db_path = Path(_BENCH_STATE_DIR) / 'index.db'
    sqlite_mb = _db_size_mb(db_path)

    # Acceptance check: sub-200ms @ 50k limit=1000
    s50k = sqlite_results.get('size_50000', {})
    p50_50k_1000 = s50k.get('search_limit_1000_p50_ms')
    sub_200 = p50_50k_1000 is not None and p50_50k_1000 < 200.0

    # Tier-3 comparison (16k = 2.2s before SQLite)
    tier3_16k_ms = 2243.0
    after_16k = sqlite_results.get('size_16000', {}).get('search_limit_1000_p50_ms')
    improvement_ratio = round(tier3_16k_ms / after_16k, 1) if after_16k else None

    # Regression check: all smaller sizes also sub-200ms
    no_regression = all(
        sqlite_results.get(f'size_{s}', {}).get('search_limit_1000_p50_ms', 999) < 200.0 for s in SIZES
    )

    verdict = 'PASS' if sub_200 else 'FAIL'

    put_phase_detail = {}
    prev = 0
    for size, elapsed in zip(SIZES, put_times):
        delta = size - prev
        put_phase_detail[f'size_{size}'] = {
            'elapsed_sec': round(elapsed, 2),
            'added': delta,
            'rate_obs_per_sec': round(delta / elapsed, 0) if elapsed > 0 else 0,
        }
        prev = size

    result = {
        'task_id': 'TASK-145',
        'measured_at': measured_at,
        'env': {
            'python': platform.python_version(),
            'sqlite': sqlite3.sqlite_version,
            'platform': platform.platform(),
            'cpu': platform.processor(),
            'zenoh_connect': os.environ.get('ZENOH_CONNECT'),
        },
        'config': {
            'sizes': SIZES,
            'project': PROJECT,
            'iterations_sqlite': ITERATIONS,
            'iterations_fallback': FALLBACK_ITERS,
        },
        'put_phase': put_phase_detail,
        'results': {
            'sqlite_first': sqlite_results,
            'zenoh_fallback': fallback,
            'rebuild_50k_sec': rebuild.get('duration_sec'),
            'rebuild_detail': rebuild,
            'subscriber_lag_ms_p50': sub_lag.get('p50_ms'),
            'subscriber_lag_detail': sub_lag,
        },
        'db_size': {
            'sqlite_mb': sqlite_mb,
        },
        'acceptance': {
            'sub_200ms_at_50k_limit_1000': sub_200,
            'p50_ms_at_50k_limit_1000': p50_50k_1000,
            'no_regression_smaller_sizes': no_regression,
        },
        'verdict': verdict,
        'comparison_with_tier3': {
            '16k_before_ms': tier3_16k_ms,
            '16k_after_ms': after_16k,
            'improvement_ratio': improvement_ratio,
        },
    }

    print(f'\n{"=" * 50}', flush=True)
    print(f'VERDICT: {verdict}', flush=True)
    if p50_50k_1000 is not None:
        status = 'OK' if sub_200 else 'FAIL'
        print(
            f'search_observations(limit=1000) @ 50k obs: p50={p50_50k_1000:.2f}ms  [{status}] (target: <200ms)',
            flush=True,
        )
    if improvement_ratio is not None:
        print(
            f'vs Tier-3 (16k=2243ms): now {after_16k:.2f}ms → {improvement_ratio}× faster',
            flush=True,
        )
    print(f'SQLite DB size: {sqlite_mb} MB', flush=True)
    print(f'{"=" * 50}', flush=True)

    out_path = Path('/home/gisen/work/tmux-multi-agents/docs/poc-reports/raw/TASK-145-tier4-bench-result.yaml')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as f:
        yaml.dump(result, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f'\nResult written to {out_path}', flush=True)

    return 0 if sub_200 else 1


if __name__ == '__main__':
    sys.exit(main())
