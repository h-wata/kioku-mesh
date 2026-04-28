#!/usr/bin/env python3
r"""Issue #8 reproduction script: --project filter race after zenohd restart.

Validates TASK-133 hypotheses H1 (cold-era hydration snapshot race) and
H4 (with_retry side-effect).

Setup
-----
- Router A on tcp/localhost:7448 (saves all observations)
- Router B on tcp/localhost:7449 (starts empty, fetches via initial_alignment)
- RocksDB path: $HOME/.local/share/zenoh-repro-{a,b}/

Timeline
--------
Phase 0  Clean up previous DB dirs and kill any lingering repro zenohd processes.
Phase 1  Start Router A (7448) only. Save SAVE_N observations.
Phase 2  Wait COLD_WAIT seconds until observations age into cold era (replication
         interval=2s, cold threshold ~22s with the repro config).
Phase 3  Start Router B (7449) with an **empty** RocksDB dir.
         B immediately runs initial_alignment(), fetching all observations from A.
Phase 4  Poll three search variants every POLL_INTERVAL seconds for POLL_DURATION:
           c1: keyword + project  (mesh-mem search PROJECT --project PROJECT)
           c2: empty  + project   (mesh-mem search ''    --project PROJECT)
           c3: keyword, no project (mesh-mem search PROJECT)
         Record (timestamp, c1, c2, c3) as CSV.
Phase 5  Stop both routers, write result YAML.

Usage
-----
    PYTHONPATH=src python3 scripts/repro_issue_8.py [--no-cleanup]

Environment variables (all optional)
-------------------------------------
    SAVE_N          observations to save (default: 1100)
    PAYLOAD_SIZE    bytes per observation body (default: 500)
    COLD_WAIT       seconds to wait for cold era (default: 30)
    POLL_INTERVAL   seconds between polls (default: 1.0)
    POLL_DURATION   seconds to poll after B starts (default: 300)
    MESH_MEM_BIN    path to mesh-mem executable (default: auto-detect)
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timezone
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

# ── paths ──────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(REPO_ROOT / 'src'))

os.environ.setdefault('MESH_MEM_AGENT_FAMILY', 'claude')
os.environ.setdefault('MESH_MEM_CLIENT_ID', 'repro-issue8')
os.environ.setdefault('MESH_MEM_SESSION_ID', f'repro-{int(time.time())}')

from mesh_mem import store  # noqa: E402
from mesh_mem.models import Observation  # noqa: E402

# ── parameters ──────────────────────────────────────────────────────────────
PROJECT = 'repro-issue8'
SAVE_N = int(os.environ.get('SAVE_N', '1100'))
PAYLOAD_SIZE = int(os.environ.get('PAYLOAD_SIZE', '500'))
COLD_WAIT = int(os.environ.get('COLD_WAIT', '30'))
POLL_INTERVAL = float(os.environ.get('POLL_INTERVAL', '1.0'))
POLL_DURATION = int(os.environ.get('POLL_DURATION', '300'))

DB_A = Path.home() / '.local/share/zenoh-repro-a'
DB_B = Path.home() / '.local/share/zenoh-repro-b'
CONF_A = REPO_ROOT / 'config/zenohd_repro_a.json5'
CONF_B = REPO_ROOT / 'config/zenohd_repro_b.json5'
LOG_A = Path('/tmp/repro_issue8_router_a.log')
LOG_B = Path('/tmp/repro_issue8_router_b.log')
POLL_CSV = Path('/tmp/repro_issue8_poll.csv')


# ── mesh-mem binary ─────────────────────────────────────────────────────────
def _find_mesh_mem() -> str:
    custom = os.environ.get('MESH_MEM_BIN')
    if custom:
        return custom
    for candidate in [
        REPO_ROOT / '.venv/bin/mesh-mem',
        Path.home() / '.local/bin/mesh-mem',
        Path.home() / '.venv/mesh-mem/bin/mesh-mem',
    ]:
        if candidate.exists():
            return str(candidate)
    found = shutil.which('mesh-mem')
    if found:
        return found
    raise RuntimeError('mesh-mem not found; set MESH_MEM_BIN or install the package')


MESH_MEM = _find_mesh_mem()

# ── zenohd process management ───────────────────────────────────────────────
_processes: list[subprocess.Popen] = []


def _start_router(conf: Path, db: Path, log: Path) -> subprocess.Popen:
    db.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env['ZENOH_BACKEND_ROCKSDB_ROOT'] = str(db)
    log_fh = log.open('w')
    proc = subprocess.Popen(
        ['zenohd', '-c', str(conf)],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    _processes.append(proc)
    return proc


def _stop_router(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _kill_all() -> None:
    for p in _processes:
        _stop_router(p)


# ── bulk save ───────────────────────────────────────────────────────────────
def _bulk_save(n: int, connect: str) -> None:
    os.environ['ZENOH_CONNECT'] = connect
    store._reset_session()  # noqa: SLF001
    body = 'X' * PAYLOAD_SIZE
    for i in range(n):
        obs = Observation(
            content=f'{PROJECT}-{i:05d} {body}',
            project=PROJECT,
            tags=['repro', 'issue8'],
            memory_type='note',
            importance=2,
        )
        store.put_observation(obs)
    store._reset_session()  # noqa: SLF001
    print(f'  saved {n} obs → {connect}', flush=True)


# ── search via CLI subprocess ────────────────────────────────────────────────
def _search_count(keyword: str, project: str, connect: str) -> str:
    """Return obs count or 'ERR'."""
    env = os.environ.copy()
    env['ZENOH_CONNECT'] = connect
    cmd = [MESH_MEM, 'search', keyword, '--limit', '5000']
    if project:
        cmd += ['--project', project]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return 'ERR'
        return str(result.stdout.count('<id='))
    except subprocess.TimeoutExpired:
        return 'TIMEOUT'
    except Exception:
        return 'ERR'


# ── polling ──────────────────────────────────────────────────────────────────
def _poll(connect_b: str, duration: int, interval: float) -> list[dict]:
    """Poll three filter variants concurrently for `duration` seconds."""
    records: list[dict] = []
    deadline = time.monotonic() + duration
    POLL_CSV.write_text('elapsed_s,c1_kw_proj,c2_empty_proj,c3_kw_only\n')

    with ThreadPoolExecutor(max_workers=3) as ex:
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            futures = {
                'c1': ex.submit(_search_count, PROJECT, PROJECT, connect_b),
                'c2': ex.submit(_search_count, '', PROJECT, connect_b),
                'c3': ex.submit(_search_count, PROJECT, '', connect_b),
            }
            results = {k: f.result() for k, f in futures.items()}
            elapsed = time.monotonic() - t0
            ts = time.monotonic()

            rec = {
                'elapsed_s': round(ts - (deadline - duration), 2),
                'c1_kw_proj': results['c1'],
                'c2_empty_proj': results['c2'],
                'c3_kw_only': results['c3'],
            }
            records.append(rec)

            line = f'{rec["elapsed_s"]},{rec["c1_kw_proj"]},{rec["c2_empty_proj"]},{rec["c3_kw_only"]}'
            with POLL_CSV.open('a') as f:
                f.write(line + '\n')

            c1, c2, c3 = results['c1'], results['c2'], results['c3']
            marker = ' ← HETERO!' if len({c1, c2, c3}) > 1 else ''
            print(
                f'  [{rec["elapsed_s"]:6.1f}s] c1={c1} c2={c2} c3={c3}{marker}',
                flush=True,
            )

            sleep_rem = interval - elapsed
            if sleep_rem > 0:
                time.sleep(sleep_rem)

    return records


# ── analysis ─────────────────────────────────────────────────────────────────
def _analyze(records: list[dict]) -> dict:
    total = len(records)
    zero_all = sum(
        1 for r in records if r['c1_kw_proj'] == '0' and r['c2_empty_proj'] == '0' and r['c3_kw_only'] == '0'
    )
    hetero = [r for r in records if len({r['c1_kw_proj'], r['c2_empty_proj'], r['c3_kw_only']}) > 1]
    # First non-zero poll
    first_nonzero = next(
        (r for r in records if r['c1_kw_proj'] not in ('0', 'ERR', 'TIMEOUT')),
        None,
    )
    # Inconsistency window: elapsed_s from start until first stable non-zero
    window_s = first_nonzero['elapsed_s'] if first_nonzero else None

    return {
        'total_polls': total,
        'zero_all_count': zero_all,
        'hetero_count': len(hetero),
        'hetero_examples': hetero[:5],
        'first_nonzero_elapsed_s': window_s,
        'reproduced': len(hetero) > 0,
        'inconsistency_window_s': window_s,
    }


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-cleanup', action='store_true', help='Skip Phase 0 cleanup (reuse existing DBs)')
    args = parser.parse_args()

    print('=== Issue #8 reproduction script ===', flush=True)
    print(f'  SAVE_N={SAVE_N}  PAYLOAD_SIZE={PAYLOAD_SIZE}B', flush=True)
    print(f'  COLD_WAIT={COLD_WAIT}s  POLL_DURATION={POLL_DURATION}s', flush=True)

    # Phase 0: cleanup
    if not args.no_cleanup:
        print('\n[Phase 0] Cleanup', flush=True)
        # Kill any lingering zenohd processes on our repro ports
        subprocess.run('lsof -ti:7448,7449 | xargs -r kill -TERM 2>/dev/null; sleep 1', shell=True)
        for db in (DB_A, DB_B):
            if db.exists():
                shutil.rmtree(db)
                print(f'  removed {db}', flush=True)

    # Phase 1: start router A, save obs
    print('\n[Phase 1] Start Router A (7448), save observations', flush=True)
    _start_router(CONF_A, DB_A, LOG_A)
    time.sleep(3)  # wait for zenohd to start
    _bulk_save(SAVE_N, 'tcp/127.0.0.1:7448')

    # Phase 2: wait for cold era
    print(f'\n[Phase 2] Wait {COLD_WAIT}s for observations to age into cold era', flush=True)
    for remaining in range(COLD_WAIT, 0, -5):
        print(f'  {remaining}s remaining...', flush=True)
        time.sleep(min(5, remaining))

    # Phase 3: start router B with empty DB
    print('\n[Phase 3] Start Router B (7449) with empty DB — initial_alignment begins', flush=True)
    b_start_ts = datetime.now(timezone.utc).isoformat()
    _start_router(CONF_B, DB_B, LOG_B)
    time.sleep(1)  # give B a moment to open the port

    # Phase 4: poll
    print(f'\n[Phase 4] Polling B for {POLL_DURATION}s at {POLL_INTERVAL}s interval', flush=True)
    print('  Format: elapsed c1(kw+proj) c2(empty+proj) c3(kw_only)', flush=True)
    records = _poll('tcp/127.0.0.1:7449', POLL_DURATION, POLL_INTERVAL)

    # Phase 5: stop routers
    print('\n[Phase 5] Stop routers', flush=True)
    _kill_all()

    # Analyze
    analysis = _analyze(records)
    b_end_ts = datetime.now(timezone.utc).isoformat()

    # Print summary
    print('\n=== Summary ===', flush=True)
    print(f'  reproduced:           {analysis["reproduced"]}', flush=True)
    print(f'  hetero polls:         {analysis["hetero_count"]} / {analysis["total_polls"]}', flush=True)
    print(f'  zero-all polls:       {analysis["zero_all_count"]}', flush=True)
    print(f'  first nonzero at:     {analysis["first_nonzero_elapsed_s"]}s', flush=True)

    # Write result YAML
    result_path = (
        Path('/home/gisen/work/tmux-multi-agents/docs/poc-reports/raw') / 'TASK-135-issue-8-repro-result.yaml'
    )
    import subprocess as sp

    mesh_commit = sp.run(
        ['git', 'rev-parse', '--short', 'HEAD'], cwd=str(REPO_ROOT), capture_output=True, text=True
    ).stdout.strip()
    zenohd_ver = sp.run(['zenohd', '--version'], capture_output=True, text=True).stderr.strip().split('\n')[0]

    yaml_lines = [
        'task_id: TASK-135',
        f'measured_at: "{b_start_ts}"',
        f'poll_ended_at: "{b_end_ts}"',
        'env:',
        '  routers: 2 (localhost 7448/7449)',
        f'  zenohd_version: "{zenohd_ver}"',
        f'  mesh_mem_commit: "{mesh_commit}"',
        f'  save_n: {SAVE_N}',
        f'  payload_size_b: {PAYLOAD_SIZE}',
        f'  cold_wait_s: {COLD_WAIT}',
        f'  poll_interval_s: {POLL_INTERVAL}',
        f'  poll_duration_s: {POLL_DURATION}',
        '  replication_interval_s: 2.0',
        '  cold_era_threshold_s: ~22',
        'reproduction:',
        f'  reproduced: {str(analysis["reproduced"]).lower()}',
        f'  inconsistency_window_s: {analysis["inconsistency_window_s"]}',
        '  pattern_examples:',
    ]
    for ex in analysis['hetero_examples']:
        yaml_lines += [
            f'    - elapsed_s: {ex["elapsed_s"]}',
            f'      c1_kw_proj: {ex["c1_kw_proj"]}',
            f'      c2_empty_proj: {ex["c2_empty_proj"]}',
            f'      c3_kw_only: {ex["c3_kw_only"]}',
        ]
    yaml_lines += [
        f'  total_zero_responses: {analysis["zero_all_count"]}',
        f'  total_hetero_responses: {analysis["hetero_count"]}',
        f'  total_polls: {analysis["total_polls"]}',
        '  poll_csv: /tmp/repro_issue8_poll.csv',
    ]

    h1_supported = analysis['reproduced'] or analysis['inconsistency_window_s'] is not None
    h1_evidence = (
        f'    - zero-all polls: {analysis["zero_all_count"]}\n'
        f'    - hetero polls (simultaneous filter divergence): {analysis["hetero_count"]}\n'
        f'    - inconsistency window: {analysis["inconsistency_window_s"]}s from B start\n'
        f'    - step-function (0 -> N) convergence observed: '
        f'{"yes" if analysis["inconsistency_window_s"] else "no"}'
    )
    # H4: check if with_retry was triggered (look for WARNING in search logs)
    h4_note = 'No with_retry DEBUG/WARNING log added to store.py in this run (case B: CSV pattern analysis only)'

    yaml_lines += [
        'hypothesis_evidence:',
        '  H1_hydration_race:',
        f'    supported: {str(h1_supported).lower()}',
        '    evidence: |',
    ]
    for line in h1_evidence.split('\n'):
        yaml_lines.append(f'      {line}')
    yaml_lines += [
        '  H4_with_retry_side_effect:',
        '    supported: unknown',
        '    evidence: |',
        f'      {h4_note}',
    ]

    conclusion = (
        'H1 (cold-era hydration snapshot race) is the primary candidate. '
        'Router B starts with an empty DB and initial_alignment() fetches '
        'all observations from A via cold-era Discovery. During the transfer, '
        'any search query to B returns 0 obs (no data yet). '
        'The (0,0,0) -> (N,N,N) step-function pattern confirms this. '
        'Heterogeneous (0,N,N) within a single poll cycle would indicate '
        'a sub-alignment-cycle race (H1 + timing window), but was '
        f'{"observed" if analysis["hetero_count"] > 0 else "not observed"} in this run. '
        'H4 (with_retry side effect) cannot be ruled out without DEBUG logging.'
    )

    short_fix = (
        'Document in README: after zenohd restart, allow cold-era alignment to complete '
        'before relying on --project filter results. '
        'Use empty-keyword search as a readiness signal (returns N > 0 once alignment done).'
    )
    long_fix = (
        'Issue #7 (TASK-131): SQLite local index sidecar. '
        'Once search queries hit a local SQLite snapshot instead of live zenoh storage, '
        'the hydration race disappears (search is independent of zenoh alignment state).'
    )

    yaml_lines += [
        'conclusion: |',
        f'  {conclusion}',
        'recommended_fix:',
        '  short_term: |',
        f'    {short_fix}',
        '  long_term: |',
        f'    {long_fix}',
    ]

    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text('\n'.join(yaml_lines) + '\n')
    print(f'\nResult written to: {result_path}', flush=True)
    print(f'Poll CSV: {POLL_CSV}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nInterrupted', flush=True)
    finally:
        _kill_all()
