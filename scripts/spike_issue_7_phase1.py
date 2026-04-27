#!/usr/bin/env python3
"""Issue #7 Phase 1 spike: measure SQLite rebuild + query latency at 50k obs.

Validates the assumption that SQLite WHERE+LIMIT stays sub-10ms at 50k rows
(see TASK-131 design doc, plan B "SQLite local index").

This is a throwaway spike; no zenoh I/O is involved. We generate Observation
objects in memory, bulk-insert them into a fresh SQLite file, and time:

    1a) rebuild     — bulk INSERT 50k rows (BEGIN..COMMIT, executemany)
    1b) query       — SELECT ... WHERE project=? ORDER BY created_at DESC LIMIT 50
    1c) deserialize — rehydrate 50 rows back into Observation instances
    1d) DB size     — final .db file size in MB

Run:
    PYTHONPATH=src python3 scripts/spike_issue_7_phase1.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))

from mesh_mem.models import Observation  # noqa: E402

DB_PATH = Path('/tmp/spike_issue_7.db')
N_OBS = 50_000
PROJECTS = ['proj-A', 'proj-B', 'proj-C', 'proj-D', 'proj-E']
QUERY_ITERATIONS = 1000
QUERY_LIMIT = 50

SCHEMA = """
CREATE TABLE obs_index (
  observation_id TEXT PRIMARY KEY,
  project TEXT,
  created_at TEXT,
  memory_type TEXT,
  importance INTEGER,
  subject TEXT,
  summary TEXT,
  payload_json TEXT
);
CREATE INDEX idx_project_created ON obs_index(project, created_at DESC);
CREATE INDEX idx_created ON obs_index(created_at DESC);
"""


def _generate_observations(n: int) -> list[Observation]:
    obs_list: list[Observation] = []
    base_ts = 1_700_000_000  # arbitrary epoch (seconds)
    for i in range(n):
        # Spread created_at over a wide range; younger rows get larger ts.
        # Format matches Observation._utc_now_iso (ISO 8601 'Z').
        ts = base_ts + i
        # Deterministic ISO string sort-equivalent to numeric ts ordering.
        secs = ts % 60
        mins = (ts // 60) % 60
        hours = (ts // 3600) % 24
        days = (ts // 86400) % 28 + 1
        created_at = f'2026-01-{days:02d}T{hours:02d}:{mins:02d}:{secs:02d}.{i % 1000:03d}000Z'
        project = PROJECTS[i % len(PROJECTS)]
        obs_list.append(
            Observation(
                content=f'spike content {i:06d} ' + 'X' * 200,
                project=project,
                tags=[f'tag-{i % 17}', 'spike'],
                created_at=created_at,
                memory_type='note' if i % 3 else 'fact',
                importance=(i % 5) + 1,
                subject=f'subject-{i % 137}',
                summary=f'summary line for row {i}',
            )
        )
    return obs_list


def _row_from_obs(obs: Observation) -> tuple:
    return (
        obs.observation_id,
        obs.project,
        obs.created_at,
        obs.memory_type,
        obs.importance,
        obs.subject,
        obs.summary,
        obs.to_json(),
    )


def _open_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.executescript(SCHEMA)
    return conn


def _phase_1a_rebuild(conn: sqlite3.Connection, obs_list: list[Observation]) -> float:
    rows = [_row_from_obs(o) for o in obs_list]
    start = time.perf_counter()
    conn.execute('BEGIN')
    conn.executemany(
        'INSERT INTO obs_index '
        '(observation_id, project, created_at, memory_type, importance, subject, summary, payload_json) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        rows,
    )
    conn.commit()
    return time.perf_counter() - start


def _phase_1b_query(conn: sqlite3.Connection, iterations: int) -> dict:
    sql = 'SELECT observation_id, payload_json FROM obs_index WHERE project = ? ORDER BY created_at DESC LIMIT ?'
    samples_ms: list[float] = []
    # Warm cache once
    conn.execute(sql, ('proj-A', QUERY_LIMIT)).fetchall()
    for i in range(iterations):
        proj = PROJECTS[i % len(PROJECTS)]
        t0 = time.perf_counter()
        rows = conn.execute(sql, (proj, QUERY_LIMIT)).fetchall()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        samples_ms.append(elapsed_ms)
        if len(rows) != QUERY_LIMIT:
            raise RuntimeError(f'expected {QUERY_LIMIT} rows for {proj}, got {len(rows)}')
    samples_ms.sort()
    return {
        'p50_ms': statistics.median(samples_ms),
        'p99_ms': samples_ms[int(len(samples_ms) * 0.99)],
        'max_ms': samples_ms[-1],
        'min_ms': samples_ms[0],
        'mean_ms': statistics.mean(samples_ms),
        'iterations': iterations,
    }


def _phase_1c_deserialize(conn: sqlite3.Connection) -> float:
    rows = conn.execute(
        'SELECT payload_json FROM obs_index WHERE project = ? ORDER BY created_at DESC LIMIT ?',
        ('proj-A', QUERY_LIMIT),
    ).fetchall()
    start = time.perf_counter()
    decoded = [Observation.from_json(r[0]) for r in rows]
    elapsed = time.perf_counter() - start
    if len(decoded) != QUERY_LIMIT:
        raise RuntimeError(f'deserialize: expected {QUERY_LIMIT}, got {len(decoded)}')
    return elapsed


def _phase_1d_size(path: Path) -> float:
    total = 0
    for suffix in ('', '-wal', '-shm'):
        p = Path(str(path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total / (1024 * 1024)


def main() -> dict:
    print(f'[spike] generating {N_OBS:,} Observation objects...', flush=True)
    t0 = time.perf_counter()
    obs_list = _generate_observations(N_OBS)
    gen_sec = time.perf_counter() - t0
    print(f'[spike] generated in {gen_sec:.2f}s', flush=True)

    print(f'[spike] opening DB at {DB_PATH}', flush=True)
    conn = _open_db(DB_PATH)
    try:
        print('[spike] phase 1a: bulk insert (rebuild)...', flush=True)
        rebuild_sec = _phase_1a_rebuild(conn, obs_list)
        print(f'[spike]   rebuild: {rebuild_sec:.2f}s', flush=True)

        print(f'[spike] phase 1b: query x{QUERY_ITERATIONS}...', flush=True)
        q_stats = _phase_1b_query(conn, QUERY_ITERATIONS)
        print(
            f'[spike]   p50={q_stats["p50_ms"]:.3f}ms p99={q_stats["p99_ms"]:.3f}ms max={q_stats["max_ms"]:.3f}ms',
            flush=True,
        )

        print('[spike] phase 1c: deserialize 50 rows...', flush=True)
        deser_sec = _phase_1c_deserialize(conn)
        print(f'[spike]   deserialize: {deser_sec * 1000:.3f}ms', flush=True)
    finally:
        conn.close()

    db_size_mb = _phase_1d_size(DB_PATH)
    print(f'[spike] phase 1d: db size = {db_size_mb:.2f} MB', flush=True)

    total_search_ms = q_stats['p50_ms'] + (deser_sec * 1000.0)

    rebuild_pass = rebuild_sec < 30.0
    query_p50_pass = q_stats['p50_ms'] < 5.0
    query_p99_pass = q_stats['p99_ms'] < 20.0
    deser_pass = (deser_sec * 1000.0) < 10.0
    size_pass = db_size_mb < 100.0

    all_pass = all([rebuild_pass, query_p50_pass, query_p99_pass, deser_pass, size_pass])
    if all_pass:
        verdict = 'GO'
    elif rebuild_pass and query_p50_pass and query_p99_pass:
        verdict = 'CONDITIONAL_GO'
    else:
        verdict = 'NO_GO'

    result = {
        'n_obs': N_OBS,
        'env': {
            'python': platform.python_version(),
            'sqlite': sqlite3.sqlite_version,
            'platform': platform.platform(),
            'machine': platform.machine(),
        },
        'rebuild': {
            'duration_sec': round(rebuild_sec, 4),
            'rows_per_sec': round(N_OBS / rebuild_sec, 1),
            'passed_30s_target': rebuild_pass,
        },
        'query': {
            'iterations': q_stats['iterations'],
            'p50_ms': round(q_stats['p50_ms'], 4),
            'p99_ms': round(q_stats['p99_ms'], 4),
            'max_ms': round(q_stats['max_ms'], 4),
            'mean_ms': round(q_stats['mean_ms'], 4),
            'passed_p50_5ms_target': query_p50_pass,
            'passed_p99_20ms_target': query_p99_pass,
        },
        'deserialize_50': {
            'duration_ms': round(deser_sec * 1000.0, 4),
            'passed_10ms_target': deser_pass,
        },
        'total_search_estimate_ms': round(total_search_ms, 4),
        'db_size_mb': round(db_size_mb, 2),
        'db_size_passed_100mb_target': size_pass,
        'verdict': verdict,
    }

    print('\n[spike] === result ===')
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if os.environ.get('SPIKE_KEEP_DB') != '1':
        for suffix in ('', '-wal', '-shm'):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
        print(f'[spike] cleaned up {DB_PATH}*')
    return result


if __name__ == '__main__':
    main()
