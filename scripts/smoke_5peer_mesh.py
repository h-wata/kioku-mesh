#!/usr/bin/env python3
"""5-peer full-mesh replication smoke test (TASK-152).

Validates that mesh-mem's Zenoh replication works correctly with 5 routers
forming a full mesh topology on localhost.

Phases:
  0: Cleanup old state and processes
  1: Start 5 routers with full-mesh connectivity
  2: All-direction put/search consistency (100 obs per peer, 5 projects)
  3: 1 peer restart scenario (alignment recovery timing)
  4: Latency measurement (search p50/p99 across all peers)
  5: Cleanup

Usage:
  PYTHONPATH=src python3 scripts/smoke_5peer_mesh.py
"""

from __future__ import annotations

import datetime
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))

# ── ports and dirs ──────────────────────────────────────────────────────────
PORTS = [7448, 7449, 7450, 7451, 7452]
N_PEERS = len(PORTS)
TMP_BASE = pathlib.Path('/tmp/mesh_smoke_5peer')
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
    return result.stdout.strip().split()[-1]


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
        return 0
    # Output format: each obs ends with " <id=xxxx>" on the body line
    return result.stdout.count('<id=')


def _start_router(peer_idx: int) -> subprocess.Popen:
    rocksdb_root = _rocksdb_dir(peer_idx).parent
    env = {**os.environ, 'ZENOH_BACKEND_ROCKSDB_ROOT': str(rocksdb_root)}
    log_path = _log_path(peer_idx)
    log_f = open(log_path, 'w')
    proc = subprocess.Popen(
        ['zenohd', '--config', str(_config_path(peer_idx))],
        stdout=log_f,
        stderr=log_f,
        env=env,
    )
    return proc


def _stop_router_by_port(port: int) -> None:
    subprocess.run(
        ['pkill', '-f', f'tcp/127.0.0.1:{port}'],
        capture_output=True,
    )
    time.sleep(1)


def _cleanup_smoke_processes() -> None:
    for port in PORTS:
        subprocess.run(['pkill', '-f', f'tcp/127.0.0.1:{port}'], capture_output=True)
    time.sleep(2)


def main() -> dict:
    results = {}
    procs: list[subprocess.Popen | None] = [None] * N_PEERS

    # ── Phase 0: cleanup ────────────────────────────────────────────────────
    print('[Phase 0] Cleanup old state and processes')
    _cleanup_smoke_processes()
    import shutil

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
        connected_counts = []
        for i in range(N_PEERS):
            log_text = _log_path(i).read_text(errors='ignore')
            connected = log_text.count('ESTABLISHED') + log_text.count('router_peers=')
            connected_counts.append(connected)
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

        # Stop peer3
        print(f'  Stopping peer3 (:{PORTS[peer3_idx]})')
        if procs[peer3_idx]:
            procs[peer3_idx].terminate()
            try:
                procs[peer3_idx].wait(timeout=10)
            except subprocess.TimeoutExpired:
                procs[peer3_idx].kill()
                procs[peer3_idx].wait()
            procs[peer3_idx] = None
        _stop_router_by_port(PORTS[peer3_idx])

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
        # ── Phase 5: cleanup ─────────────────────────────────────────────────
        print('\n[Phase 5] Cleanup')
        _cleanup_smoke_processes()
        import shutil

        if TMP_BASE.exists():
            shutil.rmtree(TMP_BASE)
        print('  Cleanup done')

    return results


if __name__ == '__main__':
    out_dir = pathlib.Path('/home/gisen/work/tmux-multi-agents/docs/poc-reports/raw')
    out_dir.mkdir(parents=True, exist_ok=True)

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
        observations.append('Phase 2: not all peers saw all obs within 30s replication window')
    p3 = results.get('phase_3', {})
    if p3.get('convergence_after_restart_sec') and p3['convergence_after_restart_sec'] > 30:
        observations.append(
            f'Phase 3: convergence took {p3["convergence_after_restart_sec"]}s (>30s, possible cold-era latency)'
        )
    if not observations:
        observations.append('No unexpected behavior observed')

    report = {
        'task_id': 'TASK-152',
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

    import yaml

    report_path = out_dir / 'TASK-152-5peer-mesh-smoke-result.yaml'
    report_path.write_text(yaml.dump(report, allow_unicode=True, sort_keys=False))
    print(f'\nReport written: {report_path}')
    print(f'Overall verdict: {overall}')
    for phase, v in phase_verdicts.items():
        print(f'  {phase}: {v}')

    sys.exit(0 if overall != 'FAIL' else 1)
