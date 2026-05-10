#!/usr/bin/env python3
"""Purge observations whose memory_type is not in the v0.2.3 closed enum.

Issue #39 / Office-host follow-up: v0.2.2 PROACTIVE SAVE writes landed with
free-form ``memory_type`` values (``fact`` / ``discovery`` / ``status`` /
``learning`` / ``bugfix`` ...) that the v0.2.3 closed enum no longer
accepts. ``Observation.from_json`` clamps these to ``"note"`` at read
time and emits a per-record WARNING, which spams stderr by the thousands
during full-scan paths (rebuild / legacy gc fallback).

This script scans ``mem/obs/**`` directly (without going through the
clamping ``from_json``) and calls :func:`store.physical_delete_observation`
for each obs whose RAW ``memory_type`` is outside ``VALID_MEMORY_TYPES``.

Usage:
    # dry-run: print count + memory_type histogram, no deletes
    python scripts/cleanup_legacy_memory_types.py

    # actually delete:
    python scripts/cleanup_legacy_memory_types.py --execute

The script must be able to reach ``zenohd`` via the same ``ZENOH_CONNECT``
the rest of the toolchain uses (defaults to ``tcp/localhost:7447``).
``MESH_MEM_SKIP_REBUILD=1`` is set internally so the index does not
trigger its own rebuild during ``physical_delete_observation``.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import logging
import os
import sys
import time

# Force CLI-style policy so get_index() does not trigger a rebuild while
# we drive deletes; we own the scan loop here.
os.environ.setdefault('MESH_MEM_SKIP_REBUILD', '1')

# Quiet the per-record clamping WARNING that this script is itself trying
# to eliminate — otherwise the dry-run output is buried in noise.
logging.getLogger('mesh_mem.models').setLevel(logging.ERROR)

from mesh_mem import store  # noqa: E402
from mesh_mem.models import VALID_MEMORY_TYPES  # noqa: E402

_PROGRESS_EVERY = 100


def _scan_legacy_obs() -> list[tuple[str, str, str]]:
    """Return ``(obs_key, observation_id, raw_memory_type)`` for legacy obs.

    Reads ``mem/obs/**`` from Zenoh, parses each payload as raw JSON, and
    keeps entries whose ``memory_type`` is not in ``VALID_MEMORY_TYPES``.
    Malformed JSON is skipped (would already be a separate problem).
    """
    session = store.get_session()
    out: list[tuple[str, str, str]] = []
    for reply in session.get('mem/obs/**', timeout=30.0):
        if not reply.ok:
            continue
        try:
            payload = json.loads(reply.ok.payload.to_string())
        except (json.JSONDecodeError, ValueError):
            continue
        raw_type = payload.get('memory_type')
        if raw_type is None or raw_type in VALID_MEMORY_TYPES:
            continue
        obs_id = payload.get('observation_id')
        if not isinstance(obs_id, str) or len(obs_id) != 32:
            continue
        out.append((str(reply.ok.key_expr), obs_id, raw_type))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--execute',
        action='store_true',
        help='Actually issue physical_delete_observation. Default is dry-run.',
    )
    args = parser.parse_args(argv)

    print('Scanning mem/obs/** for legacy memory_type values...', file=sys.stderr)
    t0 = time.monotonic()
    legacy = _scan_legacy_obs()
    elapsed = time.monotonic() - t0
    print(f'Scan complete in {elapsed:.1f}s. Legacy obs found: {len(legacy)}', file=sys.stderr)

    if not legacy:
        print('Nothing to do — no legacy memory_type values present.')
        return 0

    histogram: Counter[str] = Counter(item[2] for item in legacy)
    print('\nMemory_type histogram (raw values):', file=sys.stderr)
    for raw_type, count in histogram.most_common():
        print(f'  {raw_type!r:>20}: {count}', file=sys.stderr)

    if not args.execute:
        print(
            '\nDry run — no deletes issued. Re-run with --execute to purge.',
            file=sys.stderr,
        )
        return 0

    # Bulk path: legacy obs were saved as regular records and never tombstoned,
    # so the per-id orphan-tomb sweep that physical_delete_observation runs
    # ( ``_list_tombstones()`` over the full ``mem/tomb/**`` namespace ) is
    # both unnecessary AND a hot bottleneck — at 33k+ tombs in the mesh, each
    # scan exceeds GET_TIMEOUT and the broadcast purge retry path stalls.
    # Skip directly to exact-key delete on the obs side and mirror the
    # SQLite physical_delete so the index does not leak rows.
    print(f'\nExecuting exact-key delete for {len(legacy)} obs (skipping tomb sweep)...', file=sys.stderr)
    session = store.get_session()
    idx = store.get_index()
    purged_obs = 0
    failures = 0
    t0 = time.monotonic()
    for i, (obs_key, obs_id, _raw_type) in enumerate(legacy, start=1):
        try:
            session.delete(obs_key)
            if not idx.disabled:
                idx.physical_delete(obs_id)
            purged_obs += 1
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f'  failed for {obs_id}: {e}', file=sys.stderr)
        if i % _PROGRESS_EVERY == 0:
            print(
                f'  progress: {i}/{len(legacy)} (obs={purged_obs}, fail={failures})',
                file=sys.stderr,
            )

    elapsed = time.monotonic() - t0
    print(
        f'\nDone in {elapsed:.1f}s. obs_purged={purged_obs}, failures={failures}',
        file=sys.stderr,
    )
    return 0 if failures == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
