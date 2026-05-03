#!/usr/bin/env python3
"""5-peer full-mesh replication smoke test (TASK-152 / hardened in TASK-153).

Validates that mesh-mem's Zenoh replication works correctly with 5 routers
forming a full mesh topology on localhost.

POSIX-only: This script relies on lsof, SIGTERM/SIGKILL, and a POSIX temp
directory (via tempfile.gettempdir()). It is not supported on Windows. For
Windows environments, refer to the README for manual multi-host setup guidance.

Phases:
  0: Cleanup old state and processes (graceful shutdown + RocksDB LOCK wait)
  1: Start 5 routers with full-mesh connectivity
  2: All-direction put/search consistency (100 obs per peer, 5 projects)
  3: 1 peer restart scenario (alignment recovery timing)
  4: Latency measurement (search p50/p99 across all peers)
  5: Cleanup (same graceful sequence as Phase 0)

Usage:
  PYTHONPATH=src python3 scripts/smoke_5peer_mesh.py [--result-yaml PATH]
"""

from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))

# ── ports and dirs ──────────────────────────────────────────────────────────
PORTS = [7448, 7449, 7450, 7451, 7452]
N_PEERS = len(PORTS)
TMP_BASE = pathlib.Path(tempfile.gettempdir()) / 'mesh_smoke_5peer'
WAIT_STARTUP = 10.0
WAIT_REPLICATION = 30.0
WAIT_CONVERGENCE_MAX = 120.0
WAIT_CONVERGENCE_POLL = 5.0
OBS_PER_PEER = 100
PEER3_RESTART_WAIT = 60
PEER3_EXTRA_WRITES = 50
LATENCY_RUNS = 100


def _rocksdb_dir(peer_idx: int) -> pathlib.Path:
    return TMP_BASE / f'peer{peer_idx + 1}' / 'rocksdb'


def _state_dir(peer_idx: int) -> pathlib.Path:
    return TMP_BASE / f'client{peer_idx + 1}'


def _log_path(peer_idx: int) -> pathlib.Path:
    return TMP_BASE / f'zenohd_peer{peer_idx + 1}.log'


def _config_path(peer_idx: int) -> pathlib.Path:
    return TMP_BASE / f'zenohd_peer{peer_idx + 1}.json5'


def _make_router_config(peer_idx: int) -> str:
    port = PORTS[peer_idx]
    peers = [f'"tcp/127.0.0.1:{PORTS[i]}"' for i in range(N_PEERS) if i != peer_idx]
    peers_str = ', '.join(peers)
    rocksdb_dir = _rocksdb_dir(peer_idx)
    return f"""{{
  mode: "router",
  listen: {{ endpoints: ["tcp/127.0.0.1:{port}"] }},
  connect: {{ endpoints: [{peers_str}] }},
  timestamping: {{
    enabled: {{ router: true, peer: true, client: true }},
  }},
  plugins: {{
    storage_manager: {{
      volumes: {{
        rocksdb: {{}},
      }},
      storages: {{
        agent_mem: {{
          key_expr: "mem/**",
          strip_prefix: "mem",
          replication: {{
            interval: 2.0,
            sub_intervals: 5,
            hot: 6,
            warm: 30,
            propagation_delay: 250,
          }},
          volume: {{
            id: "rocksdb",
            dir: "{rocksdb_dir}",
            create_db: true,
          }},
        }},
      }},
    }},
  }},
}}"""


def _cli_save(peer_idx: int, content: str, project: str) -> str:
    port = PORTS[peer_idx]
    state = str(_state_dir(peer_idx))
    env = {
        **os.environ,
        'PYTHONPATH': str(pathlib.Path(__file__).parents[1] / 'src'),
        'ZENOH_CONNECT': f'tcp/localhost:{port}',
        'MESH_MEM_STATE_DIR': state,
        'MESH_MEM_DISABLE_INDEX': '1',
    }
    cmd = [
        sys.executable,
        '-m',
        'mesh_mem',
        'save',
        content,
        '--project',
        project,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f'save failed on peer{peer_idx + 1}: {result.stderr}')
    stdout = result.stdout
    m = re.search(r'([0-9a-f]{32})', stdout)
    if m is None:
        raise RuntimeError(f'unexpected save output: {stdout!r}')
    return m.group(1)


def _cli_search_count(peer_idx: int, project: str, limit: int = 500) -> int:
    port = PORTS[peer_idx]
    state = str(_state_dir(peer_idx))
    env = {
        **os.environ,
        'PYTHONPATH': str(pathlib.Path(__file__).parents[1] / 'src'),
        'ZENOH_CONNECT': f'tcp/localhost:{port}',
        'MESH_MEM_STATE_DIR': state,
        'MESH_MEM_DISABLE_INDEX': '1',
    }
    cmd = [
        sys.executable,
        '-m',
        'mesh_mem',
        'search',
        '',
        '--project',
        project,
        '--limit',
        str(limit),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if result.returncode != 0:
        # Surface CLI / transport failures rather than collapsing them to 0;
        # otherwise replication failure and CLI breakage look identical and
        # flaky-test triage is harder.
        raise RuntimeError(
            f'search CLI failed on peer{peer_idx + 1} (exit {result.returncode}): {result.stderr.strip()}'
        )
    # Output format: each obs ends with " <id=xxxx>" on the body line
    return result.stdout.count('<id=')


def _wait_for_rocksdb_lock_to_disappear(rocksdb_dir: pathlib.Path, timeout: float = 10.0) -> None:
    """Wait for the RocksDB LOCK file to disappear; raise RuntimeError on timeout.

    Shared by _graceful_stop_router and _cleanup_smoke_processes so both
    cleanup paths surface a stuck zenohd as a hard error rather than
    silently continuing.
    """
    lock_file = rocksdb_dir / 'LOCK'
    if not lock_file.exists():
        return
    deadline = time.monotonic() + timeout
    while lock_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if lock_file.exists():
        print(f'WARN: RocksDB LOCK still present after {timeout}s at {lock_file}')
        time.sleep(2.0)
        if lock_file.exists():
            raise RuntimeError(
                f'RocksDB LOCK at {lock_file} did not disappear after '
                f'{timeout + 2}s; previous zenohd process may be hung. '
                f'Manual cleanup required.'
            )


def _graceful_stop_router(
    proc: subprocess.Popen | None,
    rocksdb_dir: pathlib.Path,
    stop_timeout: float = 10.0,
    lock_timeout: float = 5.0,
) -> None:
    """SIGTERM → poll for exit (SIGKILL fallback) → wait for RocksDB LOCK to vanish."""
    if proc is not None:
        try:
            proc.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + stop_timeout
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.2)
            else:
                proc.kill()
                proc.wait()
        except OSError:
            pass

    _wait_for_rocksdb_lock_to_disappear(rocksdb_dir, timeout=lock_timeout)


def _kill_known_routers(procs: list[subprocess.Popen], timeout: float = 10.0) -> None:
    """Stop the routers we started, verifying by PID.

    Primary cleanup path: terminate then poll, SIGKILL only our own PIDs.
    """
    live = [p for p in procs if p is not None]
    for p in live:
        if p.poll() is None:
            p.terminate()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(p.poll() is not None for p in live):
            break
        time.sleep(0.2)
    for p in live:
        if p.poll() is None:
            p.kill()


def _kill_orphan_zenohd_on_port(port: int) -> int:
    """Kill orphan zenohd on PORT only if its cmdline contains 'zenohd'.

    Returns count killed. Used as a defensive cleanup for orphans from
    previous runs; not the primary cleanup path.
    """
    try:
        out = subprocess.check_output(['lsof', '-ti', f':{port}'], text=True)
    except subprocess.CalledProcessError:
        return 0
    killed = 0
    for pid_str in out.split():
        try:
            pid = int(pid_str)
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', 'replace')
            if 'zenohd' not in cmdline:
                continue
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            killed += 1
        except (ValueError, FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return killed


def _cleanup_smoke_processes(procs: list[subprocess.Popen | None] | None = None) -> None:
    """Stop all zenohd processes on smoke ports and wait for RocksDB to flush.

    Cleanup order:
      1. PID-primary: terminate our own started processes (_kill_known_routers).
      2. Orphan fallback: kill any remaining zenohd on smoke ports whose cmdline
         contains 'zenohd' (_kill_orphan_zenohd_on_port). Non-zenohd processes
         on those ports are never touched.
    """
    # 1. Graceful stop for known (our own) processes
    if procs:
        _kill_known_routers([p for p in procs if p is not None])

    # 2. Orphan cleanup — cmdline-verified, zenohd only
    for port in PORTS:
        _kill_orphan_zenohd_on_port(port)

    # Wait for all LOCK files under TMP_BASE to disappear
    for i in range(N_PEERS):
        _wait_for_rocksdb_lock_to_disappear(_rocksdb_dir(i), timeout=10.0)

    time.sleep(0.5)  # small extra margin


def _start_router(peer_idx: int) -> subprocess.Popen:
    rocksdb_root = _rocksdb_dir(peer_idx).parent
    env = {**os.environ, 'ZENOH_BACKEND_ROCKSDB_ROOT': str(rocksdb_root)}
    log_path = _log_path(peer_idx)
    # Open with `with` so the parent's handle is closed once Popen has
    # duped the fd into the child. The child keeps its own copy via dup2,
    # so its writes still land in the file. On Windows this matters for
    # cleanup: an open parent handle blocks delete/replace of log_path.
    with open(log_path, 'w') as log_f:
        proc = subprocess.Popen(
            ['zenohd', '--config', str(_config_path(peer_idx))],
            stdout=log_f,
            stderr=log_f,
            env=env,
        )
    return proc


def main() -> dict:
    import shutil

    results = {}
    procs: list[subprocess.Popen | None] = [None] * N_PEERS

    # ── Phase 0: cleanup ────────────────────────────────────────────────────
    print('[Phase 0] Cleanup old state and processes')
    _cleanup_smoke_processes()  # kill any stray processes from prior runs
    if TMP_BASE.exists():
        shutil.rmtree(TMP_BASE)
    TMP_BASE.mkdir(parents=True)
    for i in range(N_PEERS):
        _rocksdb_dir(i).mkdir(parents=True)
        _state_dir(i).mkdir(parents=True)

    # Write router configs
    for i in range(N_PEERS):
        _config_path(i).write_text(_make_router_config(i))

    print(f'  Configs written for {N_PEERS} peers at ports {PORTS}')
    print('  Home zenohd (port 7447) will NOT be touched')

    try:
        # ── Phase 1: start 5 routers ────────────────────────────────────────
        print(f'\n[Phase 1] Starting {N_PEERS} routers (ports {PORTS})')
        for i in range(N_PEERS):
            procs[i] = _start_router(i)
            print(f'  peer{i + 1} (:{PORTS[i]}) started (pid={procs[i].pid})')
            time.sleep(0.5)

        print(f'  Waiting {WAIT_STARTUP}s for full-mesh connections...')
        time.sleep(WAIT_STARTUP)

        # Verify connectivity from logs
        for i in range(N_PEERS):
            log_text = _log_path(i).read_text(errors='ignore')
            print(f'  peer{i + 1} log mentions: {log_text.count("ESTABLISHED")} ESTABLISHED')

        results['phase_1'] = {
            'started': N_PEERS,
            'ports': PORTS,
        }

        # ── Phase 2: all-direction put/search consistency ──────────────────
        print(f'\n[Phase 2] All-direction put/search (100 obs × {N_PEERS} peers)')
        projects = [f'p{i + 1}' for i in range(N_PEERS)]

        save_start = time.monotonic()
        for peer_idx in range(N_PEERS):
            project = projects[peer_idx]
            print(f'  Saving {OBS_PER_PEER} obs on peer{peer_idx + 1} → project={project}')
            for j in range(OBS_PER_PEER):
                _cli_save(peer_idx, f'smoke obs {j} from peer{peer_idx + 1}', project)
        save_elapsed = time.monotonic() - save_start
        total_saved = OBS_PER_PEER * N_PEERS
        print(f'  {total_saved} obs saved in {save_elapsed:.1f}s')

        print(f'  Waiting {WAIT_REPLICATION}s for replication...')
        time.sleep(WAIT_REPLICATION)

        # Search from every peer for every project
        phase2_actual: dict = {}
        all_correct = True
        for peer_idx in range(N_PEERS):
            peer_key = f'peer{peer_idx + 1}'
            phase2_actual[peer_key] = {}
            for proj in projects:
                count = _cli_search_count(peer_idx, proj)
                phase2_actual[peer_key][proj] = count
                if count != OBS_PER_PEER:
                    all_correct = False

        print('  Phase 2 result matrix:')
        for peer_key, counts in phase2_actual.items():
            print(f'    {peer_key}: {counts}')

        phase2_verdict = 'PASS' if all_correct else 'PARTIAL'
        results['phase_2'] = {
            'verdict': phase2_verdict,
            'expected_per_project': OBS_PER_PEER,
            'actual': phase2_actual,
            'converged': all_correct,
        }
        print(f'  Phase 2: {phase2_verdict}')

        # ── Phase 3: peer restart scenario ──────────────────────────────────
        print(f'\n[Phase 3] Peer restart scenario (peer3 stops {PEER3_RESTART_WAIT}s)')
        peer3_idx = 2  # 0-indexed

        print(f'  Stopping peer3 (:{PORTS[peer3_idx]})')
        _graceful_stop_router(procs[peer3_idx], _rocksdb_dir(peer3_idx))
        procs[peer3_idx] = None

        # Write +50 obs to peer1 during peer3 downtime
        print(f'  Writing {PEER3_EXTRA_WRITES} extra obs to peer1 while peer3 is down...')
        peer1_project = projects[0]  # p1
        for j in range(PEER3_EXTRA_WRITES):
            _cli_save(0, f'extra obs {j} during peer3 down', peer1_project)
        print(f'  Done. Waiting {PEER3_RESTART_WAIT - 10}s more of peer3 downtime...')
        time.sleep(max(0, PEER3_RESTART_WAIT - 10))

        # Restart peer3
        print(f'  Restarting peer3 (:{PORTS[peer3_idx]})')
        procs[peer3_idx] = _start_router(peer3_idx)
        restart_time = time.monotonic()

        # Poll for convergence
        expected_p1_count = OBS_PER_PEER + PEER3_EXTRA_WRITES  # 150
        convergence_sec = None
        final_count = 0

        print(f'  Polling peer3 for p1 count = {expected_p1_count}...')
        poll_start = time.monotonic()
        while time.monotonic() - poll_start < WAIT_CONVERGENCE_MAX:
            time.sleep(WAIT_CONVERGENCE_POLL)
            count = _cli_search_count(peer3_idx, peer1_project)
            elapsed = time.monotonic() - restart_time
            print(f'    {elapsed:.0f}s: peer3 sees p1={count}')
            if count >= expected_p1_count:
                convergence_sec = elapsed
                final_count = count
                break
            final_count = count

        if convergence_sec is None:
            phase3_verdict = 'PARTIAL'
            print(f'  Phase 3: PARTIAL (did not converge within {WAIT_CONVERGENCE_MAX}s, final={final_count})')
        else:
            phase3_verdict = 'PASS' if convergence_sec <= 30 else 'PARTIAL'
            print(f'  Phase 3: {phase3_verdict} (converged in {convergence_sec:.1f}s)')

        results['phase_3'] = {
            'verdict': phase3_verdict,
            'peer_restart_time_sec': PEER3_RESTART_WAIT,
            'additional_writes_during_partition': PEER3_EXTRA_WRITES,
            'convergence_after_restart_sec': round(convergence_sec, 1) if convergence_sec else None,
            'final_obs_count_on_peer3_p1': final_count,
            'expected': (
                f'peer1 に {OBS_PER_PEER}+{PEER3_EXTRA_WRITES}={expected_p1_count} 件、'
                f'peer3 で {expected_p1_count} 件見える'
            ),
        }

        # ── Phase 4: latency measurement ────────────────────────────────────
        print(f'\n[Phase 4] Latency measurement ({LATENCY_RUNS} × search across all peers)')
        latencies_ms: list[float] = []
        for peer_idx in range(N_PEERS):
            for _ in range(LATENCY_RUNS // N_PEERS):
                t0 = time.monotonic()
                _cli_search_count(peer_idx, projects[peer_idx], limit=50)
                latencies_ms.append((time.monotonic() - t0) * 1000)

        latencies_ms.sort()
        n = len(latencies_ms)
        p50 = latencies_ms[int(n * 0.50)]
        p99 = latencies_ms[int(n * 0.99)]
        print(f'  p50={p50:.0f}ms  p99={p99:.0f}ms  (n={n})')

        results['phase_4'] = {
            'verdict': 'PASS',
            'search_p50_ms': round(p50, 1),
            'search_p99_ms': round(p99, 1),
            'n': n,
            'notes': 'CLI subprocess overhead included; no 1-router baseline available in this run',
        }

    finally:
        # ── Phase 5: cleanup (graceful) ──────────────────────────────────────
        print('\n[Phase 5] Cleanup (graceful shutdown + RocksDB LOCK wait)')
        _cleanup_smoke_processes(procs)
        if TMP_BASE.exists():
            shutil.rmtree(TMP_BASE)
        print('  Cleanup done')

    return results


def _default_result_path(measured_at: str) -> pathlib.Path:
    out_dir = pathlib.Path.home() / 'mesh-mem-smoke-results'
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f'smoke_5peer_{measured_at.replace(":", "").replace("-", "")}.yaml'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='5-peer full-mesh replication smoke test')
    parser.add_argument(
        '--result-yaml',
        metavar='PATH',
        default=None,
        help='Path to write the result YAML (default: ~/mesh-mem-smoke-results/smoke_5peer_<ts>.yaml)',
    )
    args = parser.parse_args()

    measured_at = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    print(f'=== 5-peer mesh smoke test  {measured_at} ===\n')

    results = main()

    # Determine overall verdict
    phase_verdicts = {
        'phase_2': results.get('phase_2', {}).get('verdict', 'FAIL'),
        'phase_3': results.get('phase_3', {}).get('verdict', 'FAIL'),
        'phase_4': results.get('phase_4', {}).get('verdict', 'PASS'),
    }
    all_pass = all(v == 'PASS' for v in phase_verdicts.values())
    any_fail = any(v == 'FAIL' for v in phase_verdicts.values())
    overall = 'PASS' if all_pass else ('FAIL' if any_fail else 'PARTIAL')

    observations = []
    if results.get('phase_2', {}).get('converged') is False:
        observations.append('Phase 2: not all peers saw all obs within replication window')
    p3 = results.get('phase_3', {})
    if p3.get('convergence_after_restart_sec') and p3['convergence_after_restart_sec'] > 30:
        observations.append(
            f'Phase 3: convergence took {p3["convergence_after_restart_sec"]}s (>30s, possible cold-era latency)'
        )
    if not observations:
        observations.append('No unexpected behavior observed')

    import yaml

    report = {
        'task_id': 'smoke_5peer',
        'measured_at': measured_at,
        'env': {
            'routers': N_PEERS,
            'topology': 'full mesh (each peer connects to other 4)',
            'total_obs_per_run': OBS_PER_PEER * N_PEERS,
            'obs_per_peer': OBS_PER_PEER,
            'ports': PORTS,
        },
        'phase_2_all_peers_see_all_obs': results.get('phase_2', {}),
        'phase_3_peer_restart': results.get('phase_3', {}),
        'phase_4_latency': results.get('phase_4', {}),
        'verdict': overall,
        'observations': observations,
    }

    report_path = pathlib.Path(args.result_yaml) if args.result_yaml else _default_result_path(measured_at)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(yaml.dump(report, allow_unicode=True, sort_keys=False))
    print(f'\nReport written: {report_path}')
    print(f'Overall verdict: {overall}')
    for phase, v in phase_verdicts.items():
        print(f'  {phase}: {v}')

    sys.exit(0 if overall != 'FAIL' else 1)
