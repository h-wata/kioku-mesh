#!/usr/bin/env python3
"""Issue #9 2-router replication smoke test.

Validates that Observation structured fields replicate end-to-end across two
zenohd routers running on localhost, and that pre-Phase-1 records can be read
with default fallbacks.

Cases:
  A: New fields replicate Router1 → Router2
  B: Old-schema record readable via new code (default fallbacks)
  C: forward-compat unit test coverage confirmed (no runtime step needed)

Usage:
  PYTHONPATH=src python3 scripts/smoke_issue_9_replication.py
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / 'src'))


# ── ports and dirs ──────────────────────────────────────────────────────────
ROUTER1_PORT = 7451
ROUTER2_PORT = 7452
TMP_BASE = pathlib.Path('/tmp/smoke_task138')
ROCKSDB_R1 = TMP_BASE / 'r1'
ROCKSDB_R2 = TMP_BASE / 'r2'
ZENOHD_LOG_R1 = TMP_BASE / 'zenohd_r1.log'
ZENOHD_LOG_R2 = TMP_BASE / 'zenohd_r2.log'
CONFIG_R1 = TMP_BASE / 'zenohd_r1.json5'
CONFIG_R2 = TMP_BASE / 'zenohd_r2.json5'

PROJECT = 'smoke-issue-9'
WAIT_CONNECT = 4.0
WAIT_REPLICATION = 15.0


def _make_router_config(port: int, peer_port: int, rocksdb_dir: str) -> str:
    return f"""{{
  mode: "router",
  listen: {{ endpoints: ["tcp/127.0.0.1:{port}"] }},
  connect: {{ endpoints: ["tcp/127.0.0.1:{peer_port}"] }},
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


def _zenoh_put_old_schema(port: int, key_expr: str, payload: dict) -> None:
    """Directly put a pre-Phase-1 JSON record via zenoh-python."""
    import zenoh as z

    cfg = z.Config()
    cfg.insert_json5('mode', '"client"')
    cfg.insert_json5('connect/endpoints', f'["tcp/localhost:{port}"]')
    with z.open(cfg) as session:
        session.put(key_expr, json.dumps(payload).encode())
        time.sleep(0.5)


def _cli_save(port: int, state_dir: str, **kwargs) -> str:
    """Run mesh-mem save and return the observation_id."""
    cmd = [sys.executable, '-m', 'mesh_mem', 'save', kwargs['content']]
    for flag, val in kwargs.items():
        if flag == 'content':
            continue
        if isinstance(val, list):
            if val:
                cmd += [f'--{flag.replace("_", "-")}', ','.join(val)]
        else:
            cmd += [f'--{flag.replace("_", "-")}', str(val)]
    env = {
        **os.environ,
        'PYTHONPATH': str(pathlib.Path(__file__).parents[1] / 'src'),
        'ZENOH_CONNECT': f'tcp/localhost:{port}',
        'MESH_MEM_STATE_DIR': state_dir,
        'MESH_MEM_DISABLE_INDEX': '1',
    }
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(f'save failed: {result.stderr}')
    # parse "保存完了: <id>"
    line = result.stdout.strip()
    obs_id = line.split()[-1]
    return obs_id


def _cli_search(port: int, state_dir: str, project: str, query: str = '') -> list[dict]:
    """Run mesh-mem search and return raw output lines."""
    env = {
        **os.environ,
        'PYTHONPATH': str(pathlib.Path(__file__).parents[1] / 'src'),
        'ZENOH_CONNECT': f'tcp/localhost:{port}',
        'MESH_MEM_STATE_DIR': state_dir,
        'MESH_MEM_DISABLE_INDEX': '1',
    }
    cmd = [
        sys.executable,
        '-m',
        'mesh_mem',
        'search',
        query,
        '--project',
        project,
        '--limit',
        '20',
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
    return result.stdout.strip()


class _GetMemoryResult:
    """Parsed result from mesh-mem get-memory text output."""

    def __init__(self) -> None:
        self.observation_id = ''
        self.memory_type = 'note'
        self.importance = 2
        self.subject = ''
        self.summary = ''
        self.source_files: list[str] = []
        self.supersedes: list[str] = []
        self.content = ''


def _cli_get_memory(port: int, state_dir: str, obs_id: str) -> _GetMemoryResult | None:
    """Run mesh-mem get-memory and parse the text output into a result object."""
    env = {
        **os.environ,
        'PYTHONPATH': str(pathlib.Path(__file__).parents[1] / 'src'),
        'ZENOH_CONNECT': f'tcp/localhost:{port}',
        'MESH_MEM_STATE_DIR': state_dir,
        'MESH_MEM_DISABLE_INDEX': '1',
    }
    cmd = [sys.executable, '-m', 'mesh_mem', 'get-memory', obs_id]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
    if result.returncode != 0 or '見つかりません' in result.stderr:
        return None
    out = result.stdout
    r = _GetMemoryResult()
    for line in out.splitlines():
        if line.startswith('id: '):
            r.observation_id = line[4:].strip()
        elif line.startswith('memory_type: '):
            r.memory_type = line[13:].strip()
        elif line.startswith('importance: '):
            try:
                r.importance = int(line[12:].strip())
            except ValueError:
                pass
        elif line.startswith('subject: '):
            val = line[9:].strip()
            r.subject = '' if val == '-' else val
        elif line.startswith('summary: '):
            val = line[9:].strip()
            r.summary = '' if val == '-' else val
        elif line.startswith('source_files: '):
            val = line[14:].strip()
            r.source_files = [] if val == '-' else [v.strip() for v in val.split(',')]
        elif line.startswith('supersedes: '):
            val = line[12:].strip()
            r.supersedes = [] if val == '-' else [v.strip() for v in val.split(',')]
        elif line.startswith('---'):
            # content follows after '---'
            idx = out.index('---\n')
            r.content = out[idx + 4 :].strip()
            break
    return r if r.observation_id else None


def cleanup() -> None:
    subprocess.run(['pkill', '-f', f'tcp/127.0.0.1:{ROUTER1_PORT}'], capture_output=True)
    subprocess.run(['pkill', '-f', f'tcp/127.0.0.1:{ROUTER2_PORT}'], capture_output=True)
    time.sleep(1)


def main() -> dict:
    # ── Phase 0: setup ──────────────────────────────────────────────────────
    print('[Phase 0] Cleanup old state')
    cleanup()
    import shutil

    if TMP_BASE.exists():
        shutil.rmtree(TMP_BASE)
    TMP_BASE.mkdir(parents=True)
    ROCKSDB_R1.mkdir()
    ROCKSDB_R2.mkdir()

    # Write router configs
    CONFIG_R1.write_text(_make_router_config(ROUTER1_PORT, ROUTER2_PORT, str(ROCKSDB_R1 / 'agent_mem')))
    CONFIG_R2.write_text(_make_router_config(ROUTER2_PORT, ROUTER1_PORT, str(ROCKSDB_R2 / 'agent_mem')))

    results = {}

    try:
        # ── Phase 1: start routers ───────────────────────────────────────────
        print(f'[Phase 1] Starting Router 1 (:{ROUTER1_PORT}) and Router 2 (:{ROUTER2_PORT})')
        env_r1 = {**os.environ, 'ZENOH_BACKEND_ROCKSDB_ROOT': str(ROCKSDB_R1)}
        env_r2 = {**os.environ, 'ZENOH_BACKEND_ROCKSDB_ROOT': str(ROCKSDB_R2)}

        with open(ZENOHD_LOG_R1, 'w') as log1, open(ZENOHD_LOG_R2, 'w') as log2:
            subprocess.Popen(
                ['zenohd', '--config', str(CONFIG_R1)],
                stdout=log1,
                stderr=log1,
                env=env_r1,
            )
            time.sleep(1.5)
            subprocess.Popen(
                ['zenohd', '--config', str(CONFIG_R2)],
                stdout=log2,
                stderr=log2,
                env=env_r2,
            )

        print(f'  Waiting {WAIT_CONNECT}s for routers to connect...')
        time.sleep(WAIT_CONNECT)

        state_r1 = str(TMP_BASE / 'client_r1')
        state_r2 = str(TMP_BASE / 'client_r2')
        pathlib.Path(state_r1).mkdir()
        pathlib.Path(state_r2).mkdir()

        # ── Phase 2: Case A - save with all new fields on Router 1 ──────────
        print('[Phase 2] Case A: save full-field Observation via Router 1')
        t0 = time.monotonic()
        obs_id = _cli_save(
            ROUTER1_PORT,
            state_r1,
            content='2-router replication smoke: all new fields',
            project=PROJECT,
            memory_type='decision',
            importance=5,
            subject='replication smoke',
            summary='Router1→Router2 6-field replication verified',
            source_files=['scripts/smoke_issue_9_replication.py'],
            supersedes=[],
        )
        save_latency = time.monotonic() - t0
        print(f'  Saved id={obs_id} in {save_latency:.2f}s')

        # ── Phase 3: Case A - search/get on Router 2 ────────────────────────
        print(f'[Phase 3] Case A: waiting {WAIT_REPLICATION}s for replication, then read via Router 2')
        time.sleep(WAIT_REPLICATION)

        search_out = _cli_search(ROUTER2_PORT, state_r2, PROJECT)
        print(f'  search output: {search_out[:200]}')

        restored = _cli_get_memory(ROUTER2_PORT, state_r2, obs_id)
        if restored is None:
            case_a_passed = False
            case_a_observed = f'get-memory on Router2 returned None. search output: {search_out[:300]}'
        else:
            checks = {
                'memory_type': restored.memory_type == 'decision',
                'importance': restored.importance == 5,
                'subject': restored.subject == 'replication smoke',
                'summary': 'Router1' in restored.summary,
                'source_files': restored.source_files == ['scripts/smoke_issue_9_replication.py'],
                'supersedes': restored.supersedes == [],
            }
            case_a_passed = all(checks.values())
            case_a_observed = (
                f'Router1 save → Router2 get-memory field checks: {checks}\n'
                f'memory_type={restored.memory_type!r}, importance={restored.importance}, '
                f'subject={restored.subject!r}, summary={restored.summary!r}, '
                f'source_files={restored.source_files}, supersedes={restored.supersedes}'
            )
        print(f'  Case A: {"PASS" if case_a_passed else "FAIL"}')
        results['A'] = {'passed': case_a_passed, 'observed': case_a_observed}

        # ── Phase 4: Case B - old schema put directly ────────────────────────
        print('[Phase 4] Case B: put pre-Phase-1 record directly via zenoh-python on Router 1')
        old_obs_id = 'c' * 32
        old_key = f'mem/obs/claude/claude-code/smoke-pc/smoke-sess/{old_obs_id}'
        old_payload = {
            'content': 'old schema record before Phase-1',
            'agent_family': 'claude',
            'client_id': 'claude-code',
            'pc_id': 'smoke-pc',
            'session_id': 'smoke-sess',
            'project': PROJECT,
            'tags': ['legacy'],
            'observation_id': old_obs_id,
            'created_at': '2025-01-01T00:00:00.000000Z',
        }
        _zenoh_put_old_schema(ROUTER1_PORT, old_key, old_payload)
        print(f'  Put old-schema record id={old_obs_id}')

        print(f'  Waiting {WAIT_REPLICATION}s for replication...')
        time.sleep(WAIT_REPLICATION)

        old_restored = _cli_get_memory(ROUTER2_PORT, state_r2, old_obs_id)
        if old_restored is None:
            case_b_passed = False
            case_b_observed = 'get-memory for old-schema record returned None'
        else:
            b_checks = {
                'content': old_restored.content == 'old schema record before Phase-1',
                'memory_type_default': old_restored.memory_type == 'note',
                'importance_default': old_restored.importance == 2,
                'subject_default': old_restored.subject == '',
                'summary_default': old_restored.summary == '',
                'source_files_default': old_restored.source_files == [],
                'supersedes_default': old_restored.supersedes == [],
            }
            case_b_passed = all(b_checks.values())
            case_b_observed = (
                f'Pre-Phase-1 fixture read on Router2: {b_checks}\n'
                f'memory_type defaulted to {old_restored.memory_type!r}, '
                f'importance defaulted to {old_restored.importance}'
            )
        print(f'  Case B: {"PASS" if case_b_passed else "FAIL"}')
        results['B'] = {'passed': case_b_passed, 'observed': case_b_observed}

        # ── Phase 5: Case C - unit test confirmation (static) ────────────────
        print('[Phase 5] Case C: verify unit test coverage (no runtime step needed)')
        import subprocess as sp

        pytest_result = sp.run(
            [sys.executable, '-m', 'pytest', 'tests/test_models.py', '-k', 'compat or unknown', '-v', '--tb=short'],
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONPATH': str(pathlib.Path(__file__).parents[1] / 'src')},
        )
        case_c_passed = pytest_result.returncode == 0
        case_c_observed = (
            f'from_json unknown-field skip and old-schema compat tests '
            f'{"passed" if case_c_passed else "FAILED"}.\n'
            f'{pytest_result.stdout[-600:] if pytest_result.stdout else ""}'
        )
        print(f'  Case C: {"PASS" if case_c_passed else "FAIL"}')
        results['C'] = {'passed': case_c_passed, 'observed': case_c_observed}

    finally:
        # ── Phase 6: cleanup routers ──────────────────────────────────────────
        print('[Phase 6] Stopping routers')
        cleanup()

    return results


if __name__ == '__main__':
    import pathlib

    out_dir = pathlib.Path('/home/gisen/work/tmux-multi-agents/docs/poc-reports/raw')
    out_dir.mkdir(parents=True, exist_ok=True)

    results = main()

    all_passed = all(r['passed'] for r in results.values())
    conclusion = (
        '全 case PASS — Issue #9 acceptance criteria fully met'
        if all_passed
        else 'FAIL: ' + ', '.join(k for k, v in results.items() if not v['passed'])
    )

    report = {
        'task_id': 'TASK-138',
        'measured_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'cases': {
            'A_new_fields_both_ways': results['A'],
            'B_old_record_read': results['B'],
            'C_forward_compat_unit_test': results['C'],
        },
        'conclusion': conclusion,
    }

    import yaml

    report_path = out_dir / 'TASK-138-issue-9-replication-smoke.yaml'
    report_path.write_text(yaml.dump(report, allow_unicode=True, sort_keys=False))
    print(f'\nReport written: {report_path}')
    print(f'Conclusion: {conclusion}')

    sys.exit(0 if all_passed else 1)
