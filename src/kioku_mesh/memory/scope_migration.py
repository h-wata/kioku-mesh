"""Mesh re-PUT migration, scope inventory and host-local purge (design v3 task 5).

Three operator tools that finish the storage-scope cutover started by the
renderer (task 3):

- :func:`build_manifest` / :func:`replay_manifest` / :func:`verify_reput` move
  the existing ``mem/mesh/**`` keys out of the pre-split broad ``agent_mem``
  directory into the new clean ``mesh`` directory (design v3 B1, step 2/4/6).
- :func:`scope_inventory` reports what each host's directories and SQLite index
  still hold, per scope.
- :func:`build_purge_plan` / :func:`execute_purge` remove *host-local* copies of
  scopes this host no longer declares.

Four decisions in here come straight from the design and its review, and are
easy to undo by accident:

- **the manifest source is the old storage, not SQLite.** ``obs_index`` is a
  derived read index: it cannot vouch for tombstone completeness and it drops
  any ``mem/mesh/**`` key that is not an observation. A Zenoh get over
  ``mem/mesh/**`` lists keys and payload bytes as they are.
- **per-peer inventory needs ``consolidation=NONE`` (review N3).** The default
  (LATEST) collapses a key to one reply, so which peer holds what is
  unknowable. With NONE every replying storage answers separately and
  ``replier_id`` (zid + entity id) attributes it. The reachable-peer count is
  then checked against an expected value, because a peer that never answered
  contributes no keys to the union and would otherwise pass silently.
- **the old directory is never deleted here.** It stays as the rollback
  artifact; when it may go is the runbook's call (task 6).
- **purge is host-local and publishes no Zenoh delete.** Deleting the key would
  delete the legitimate owner's data everywhere. Copies already replicated to
  other hosts can only be removed by those hosts' owners — every purge output
  says so, because the limit is not obvious from a successful-looking run.
"""

from __future__ import annotations

import base64
from collections import Counter
from dataclasses import dataclass
from dataclasses import field
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any

from ..core import scope as scope_mod
from ..core.scope import ScopeSpec
from ..core.storage_render import LEGACY_SOURCE_DIR
from .visibility_migration import preflight_migration_target

log = logging.getLogger(__name__)

# Source selector for the manifest: intersects the old broad storage, and with
# ``strip_prefix: mem`` removed it becomes ``mesh/**`` at the backend, so only
# mesh keys come back (measured, review section 1.4).
MESH_SOURCE_SELECTOR = 'mem/mesh/**'
# Inventory probe: with the same strip prefix this becomes ``**`` at the
# backend, so it lists everything a directory holds regardless of the storage's
# own key expression. That is the point — it is how a stale copy is found.
INVENTORY_SELECTOR = 'mem/**'
GET_TIMEOUT = 10.0
MANIFEST_VERSION = 1
CHECKPOINT_VERSION = 1
DEFAULT_BATCH_SIZE = 500

PURGE_LIMIT_NOTE = (
    'host-local only: this removes copies from THIS host. No Zenoh delete is published, '
    'so copies already replicated to other hosts stay there until each host owner purges '
    'them locally. Removed directories are renamed aside, not deleted.'
)


class ScopeMigrationError(RuntimeError):
    """A migration gate refused to continue. Fail-stop: nothing was written."""


# -- manifest ------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    """One immutable key/payload pair to re-PUT."""

    key: str
    kind: str
    sha256: str
    payload_b64: str

    @property
    def payload(self) -> bytes:
        return base64.b64decode(self.payload_b64)


@dataclass(frozen=True)
class Replier:
    """One storage that answered the source get, and how many keys it held.

    Identified by ``(zid, eid)``: the zid is the router (the peer), the eid the
    storage inside it, so a transitional node's broad and mesh stores are told
    apart rather than merged.
    """

    zid: str
    eid: str
    keys: int


@dataclass(frozen=True)
class Manifest:
    """Immutable key/digest inventory of the mesh keys to move."""

    created_at: str
    selector: str
    entries: tuple[ManifestEntry, ...]
    repliers: tuple[Replier, ...]
    version: int = MANIFEST_VERSION

    @property
    def peer_zids(self) -> tuple[str, ...]:
        return tuple(sorted({r.zid for r in self.repliers if r.zid}))

    @property
    def digest(self) -> str:
        """Digest of the whole manifest, binding a checkpoint to one manifest.

        Resuming against a manifest that has since changed would re-PUT a
        different set of keys while skipping the ones the old checkpoint had
        marked done, so the digest is checked instead of trusted.
        """
        body = '\n'.join(f'{e.kind} {e.key} {e.sha256}' for e in self.entries)
        return hashlib.sha256(body.encode()).hexdigest()

    def kind_counts(self) -> Counter[str]:
        """``obs`` / ``tomb`` / ``other`` counts, kept in separate buckets (S5).

        Merging them would let a stale tombstone hide inside an observation
        count, which is precisely what the design forbids.
        """
        return Counter(e.kind for e in self.entries)


def kind_for_key(key: str) -> str:
    """Classify a ``mem/mesh/...`` key as ``obs``, ``tomb`` or ``other``."""
    parts = key.split('/')
    if len(parts) > 2 and parts[2] in ('obs', 'tomb'):
        return parts[2]
    return 'other'


@dataclass(frozen=True)
class _Reply:
    key: str
    payload: bytes
    zid: str
    eid: str


def _get_unconsolidated(session: Any, selector: str, *, timeout: float = GET_TIMEOUT) -> list[_Reply]:
    """Collect every reply to ``selector``, one per replying storage (N3).

    Replies are accumulated and returned rather than acted on in the loop, as
    ``transport._iter_ok_replies`` requires of every Zenoh scan in this
    codebase.
    """
    import zenoh

    out: list[_Reply] = []
    for reply in session.get(selector, timeout=timeout, consolidation=zenoh.ConsolidationMode.NONE):
        sample = getattr(reply, 'ok', None)
        if sample is None:
            continue
        replier = getattr(reply, 'replier_id', None)
        out.append(
            _Reply(
                key=str(sample.key_expr),
                payload=bytes(sample.payload.to_bytes()),
                zid=str(getattr(replier, 'zid', '') or ''),
                eid=str(getattr(replier, 'eid', '') or ''),
            )
        )
    return out


def build_manifest(
    session: Any,
    *,
    expected_peers: int,
    now_iso: str,
    selector: str = MESH_SOURCE_SELECTOR,
    timeout: float = GET_TIMEOUT,
) -> Manifest:
    """Build the immutable manifest of mesh keys from the old storage.

    Two fail-stop gates, both from design step 2:

    - a key answered with two different payload digests is not resolved by
      picking one; the migration stops so an operator decides.
    - ``expected_peers`` must equal the number of routers that answered. A peer
      that was unreachable contributes nothing to the union, so its
      peer-specific keys would be missing from the manifest without any other
      symptom.
    """
    replies = _get_unconsolidated(session, selector, timeout=timeout)

    digests: dict[str, str] = {}
    payloads: dict[str, bytes] = {}
    conflicts: list[str] = []
    for reply in replies:
        sha = hashlib.sha256(reply.payload).hexdigest()
        known = digests.get(reply.key)
        if known is None:
            digests[reply.key] = sha
            payloads[reply.key] = reply.payload
        elif known != sha:
            conflicts.append(reply.key)
    if conflicts:
        raise ScopeMigrationError(
            f'{len(sorted(set(conflicts)))} key(s) answered with conflicting payloads: '
            f'{", ".join(sorted(set(conflicts))[:5])}. '
            'The migration cannot pick a winner — let the peers converge (or resolve the key by hand) '
            'and re-run. Nothing was written.'
        )

    per_replier = Counter((r.zid, r.eid) for r in replies)
    repliers = tuple(Replier(zid=zid, eid=eid, keys=n) for (zid, eid), n in sorted(per_replier.items()))
    reached = sorted({r.zid for r in repliers if r.zid})
    if len(reached) != expected_peers:
        raise ScopeMigrationError(
            f'{len(reached)} peer router(s) answered {selector} but --expected-peers is {expected_peers} '
            f'(answered: {", ".join(reached) or "none"}). Keys held only by a peer that did not answer are '
            'absent from the manifest and would not be migrated. Bring every peer up (or pass the real '
            'count) and re-run. Nothing was written.'
        )

    entries = tuple(
        ManifestEntry(
            key=key,
            kind=kind_for_key(key),
            sha256=digests[key],
            payload_b64=base64.b64encode(payloads[key]).decode(),
        )
        for key in sorted(digests)
    )
    return Manifest(created_at=now_iso, selector=selector, entries=entries, repliers=repliers)


def write_manifest(manifest: Manifest, path: Path) -> None:
    """Write the manifest as one JSON document (atomically)."""
    data = {
        'version': manifest.version,
        'created_at': manifest.created_at,
        'selector': manifest.selector,
        'digest': manifest.digest,
        'kind_counts': dict(manifest.kind_counts()),
        'repliers': [{'zid': r.zid, 'eid': r.eid, 'keys': r.keys} for r in manifest.repliers],
        'entries': [
            {'key': e.key, 'kind': e.kind, 'sha256': e.sha256, 'payload_b64': e.payload_b64} for e in manifest.entries
        ],
    }
    _write_json_atomic(path, data)


def read_manifest(path: Path) -> Manifest:
    """Load a manifest written by :func:`write_manifest`."""
    data = json.loads(path.read_text(encoding='utf-8'))
    version = int(data.get('version', MANIFEST_VERSION))
    if version != MANIFEST_VERSION:
        raise ScopeMigrationError(f'manifest {path} has version {version}, expected {MANIFEST_VERSION}')
    manifest = Manifest(
        created_at=str(data.get('created_at', '')),
        selector=str(data.get('selector', MESH_SOURCE_SELECTOR)),
        entries=tuple(
            ManifestEntry(
                key=str(e['key']),
                kind=str(e.get('kind') or kind_for_key(str(e['key']))),
                sha256=str(e['sha256']),
                payload_b64=str(e['payload_b64']),
            )
            for e in data.get('entries', [])
        ),
        repliers=tuple(
            Replier(zid=str(r.get('zid', '')), eid=str(r.get('eid', '')), keys=int(r.get('keys', 0)))
            for r in data.get('repliers', [])
        ),
        version=version,
    )
    stored = str(data.get('digest', ''))
    if stored and stored != manifest.digest:
        raise ScopeMigrationError(
            f'manifest {path} is corrupt: stored digest {stored} does not match its entries ({manifest.digest})'
        )
    return manifest


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


# -- checkpointed re-PUT -------------------------------------------------------


@dataclass
class ReputCheckpoint:
    """Which manifest keys have been re-PUT, bound to one manifest digest."""

    manifest_digest: str
    done: list[str] = field(default_factory=list)
    version: int = CHECKPOINT_VERSION
    updated_at: str = ''


def save_reput_checkpoint(checkpoint: ReputCheckpoint, path: Path) -> None:
    _write_json_atomic(
        path,
        {
            'version': checkpoint.version,
            'manifest_digest': checkpoint.manifest_digest,
            'updated_at': checkpoint.updated_at,
            'done': checkpoint.done,
        },
    )


def load_reput_checkpoint(path: Path) -> ReputCheckpoint:
    data = json.loads(path.read_text(encoding='utf-8'))
    version = int(data.get('version', CHECKPOINT_VERSION))
    if version != CHECKPOINT_VERSION:
        raise ScopeMigrationError(f'checkpoint {path} has version {version}, expected {CHECKPOINT_VERSION}')
    return ReputCheckpoint(
        manifest_digest=str(data.get('manifest_digest', '')),
        done=[str(k) for k in data.get('done', [])],
        version=version,
        updated_at=str(data.get('updated_at', '')),
    )


def load_bound_checkpoint(path: Path, manifest: Manifest) -> ReputCheckpoint:
    """Load ``path`` and refuse it unless it belongs to ``manifest``.

    Shared by the real run and by ``--dry-run`` so both refuse the same
    checkpoint for the same reason (review B1).
    """
    checkpoint = load_reput_checkpoint(path)
    if checkpoint.manifest_digest != manifest.digest:
        raise ScopeMigrationError(
            f'checkpoint {path} belongs to manifest {checkpoint.manifest_digest[:12]} '
            f'but this manifest is {manifest.digest[:12]}. Resume with the manifest the run started '
            'from, or start a new run with a fresh checkpoint. Nothing was written.'
        )
    return checkpoint


@dataclass
class ReputResult:
    planned: int
    put: int
    already_done: int
    kind_counts: Counter[str] = field(default_factory=Counter)


def replay_manifest(
    manifest: Manifest,
    *,
    session: Any,
    checkpoint_path: Path,
    now_iso: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> ReputResult:
    """Re-PUT every manifest key, resumable from ``checkpoint_path``.

    Each batch is gated by :func:`~kioku_mesh.memory.visibility_migration.
    preflight_migration_target` before its first PUT, so a target with no live
    exact ``mesh`` storage (renderer not applied, zenohd not restarted, broad
    ``agent_mem`` still serving) stops the run instead of publishing into
    nowhere. The gate runs on resume too, where the batch is only the keys a
    previous run had not reached yet.

    The checkpoint is flushed after every batch and on the way out of a
    failure, so a resumed run redoes at most one batch. Re-PUT is idempotent
    (same key, same payload bytes), so redoing a batch is harmless.
    """
    if checkpoint_path.exists():
        checkpoint = load_bound_checkpoint(checkpoint_path, manifest)
    else:
        checkpoint = ReputCheckpoint(manifest_digest=manifest.digest, updated_at=now_iso)
        save_reput_checkpoint(checkpoint, checkpoint_path)

    done = set(checkpoint.done)
    pending = [e for e in manifest.entries if e.key not in done]
    result = ReputResult(planned=len(manifest.entries), put=0, already_done=len(manifest.entries) - len(pending))

    try:
        for start in range(0, len(pending), max(1, batch_size)):
            batch = pending[start : start + max(1, batch_size)]
            for key in dict.fromkeys(e.key for e in batch):
                preflight_migration_target(key, session)
            for entry in batch:
                session.put(entry.key, entry.payload)
                checkpoint.done.append(entry.key)
                result.put += 1
                result.kind_counts[entry.kind] += 1
            checkpoint.updated_at = now_iso
            save_reput_checkpoint(checkpoint, checkpoint_path)
    finally:
        checkpoint.updated_at = now_iso
        save_reput_checkpoint(checkpoint, checkpoint_path)
    return result


@dataclass
class VerifyReport:
    """Result of comparing the live mesh keys against the manifest."""

    missing: tuple[str, ...] = ()
    digest_mismatch: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    kind_counts: Counter[str] = field(default_factory=Counter)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.digest_mismatch or self.extra)


def verify_reput(
    session: Any,
    manifest: Manifest,
    *,
    selector: str = MESH_SOURCE_SELECTOR,
    timeout: float = GET_TIMEOUT,
) -> VerifyReport:
    """Compare live keys and payload digests against the manifest, per kind."""
    live: dict[str, str] = {}
    for reply in _get_unconsolidated(session, selector, timeout=timeout):
        live.setdefault(reply.key, hashlib.sha256(reply.payload).hexdigest())
    expected = {e.key: e.sha256 for e in manifest.entries}
    missing = tuple(sorted(k for k in expected if k not in live))
    mismatch = tuple(sorted(k for k, sha in expected.items() if k in live and live[k] != sha))
    extra = tuple(sorted(k for k in live if k not in expected))
    return VerifyReport(
        missing=missing,
        digest_mismatch=mismatch,
        extra=extra,
        kind_counts=Counter(kind_for_key(k) for k in live),
    )


# -- inventory -----------------------------------------------------------------


def label_for_key(key: str) -> str:
    """Scope label owning ``key``: ``mesh`` / ``user/x`` / ``legacy`` / ``other``."""
    spec = scope_mod.scope_from_key(key)
    if spec is not None:
        return spec.label
    parts = key.split('/')
    if len(parts) > 1 and parts[0] == 'mem' and parts[1] in ('obs', 'tomb'):
        return 'legacy'
    return 'other'


def kind_for_scoped_key(key: str, label: str) -> str:
    """``obs`` / ``tomb`` / ``other`` for any key, not just ``mem/mesh/**``."""
    parts = key.split('/')
    if label == 'legacy':
        return parts[1] if len(parts) > 1 and parts[1] in ('obs', 'tomb') else 'other'
    prefix_len = len(f'mem/{label}'.split('/'))
    if len(parts) > prefix_len and parts[prefix_len] in ('obs', 'tomb'):
        return parts[prefix_len]
    return 'other'


def probe_inventory(
    session: Any,
    *,
    selector: str = INVENTORY_SELECTOR,
    self_only: bool = True,
    timeout: float = GET_TIMEOUT,
) -> dict[str, Counter[str]]:
    """Count what this host's directories hold, per scope label and kind.

    ``self_only`` keeps the answer host-local by dropping replies from other
    routers — the question this command answers is "what is on *this* machine",
    and a peer's copy showing up here would read as a local leftover.
    Tombstones are counted from here rather than from SQLite: a tombstone whose
    observation row is absent has no ``obs_index`` row to be counted in.
    """
    zids: set[str] = set()
    if self_only:
        try:
            zids = {scope_mod.self_router_zid(session)}
        except Exception as e:  # noqa: BLE001 — without a zid, report every reply and say so
            log.warning('probe_inventory: cannot resolve the local router zid (%s); counting all repliers', e)
    out: dict[str, Counter[str]] = {}
    seen: set[tuple[str, str]] = set()
    for reply in _get_unconsolidated(session, selector, timeout=timeout):
        if zids and reply.zid not in zids:
            continue
        # One storage may answer a key twice (transitional overlap); count the
        # key once per storage so the totals match the directories.
        if (reply.eid, reply.key) in seen:
            continue
        seen.add((reply.eid, reply.key))
        label = label_for_key(reply.key)
        out.setdefault(label, Counter())[kind_for_scoped_key(reply.key, label)] += 1
    return out


def payload_scope_states(db_path: str | Path) -> dict[str, dict[str, int]]:
    """Per-scope live / deleted / shadowed row counts from ``obs_index``.

    Read-only (``mode=ro``) on the SQLite index, parsing ``payload_json`` — the
    design decided against a schema migration, and a full parse of ~1500 rows
    is fast enough that it never became a reason to add columns.
    """
    path = Path(db_path)
    if not path.exists():
        return {}
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    except sqlite3.Error as e:
        log.warning('payload_scope_states: cannot open %s read-only: %s', path, e)
        return {}
    try:
        rows = con.execute(
            "SELECT COALESCE(json_extract(payload_json, '$.visibility'), ''), "
            "COALESCE(json_extract(payload_json, '$.scope_id'), ''), "
            'SUM(CASE WHEN deleted_at IS NULL AND shadowed_at IS NULL THEN 1 ELSE 0 END), '
            'SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END), '
            'SUM(CASE WHEN deleted_at IS NULL AND shadowed_at IS NOT NULL THEN 1 ELSE 0 END) '
            'FROM obs_index GROUP BY 1, 2'
        ).fetchall()
    except sqlite3.Error as e:
        log.warning('payload_scope_states: query failed on %s: %s', path, e)
        return {}
    finally:
        con.close()
    out: dict[str, dict[str, int]] = {}
    for visibility, scope_id, live, deleted, shadowed in rows:
        if not visibility:
            label = 'legacy'
        elif scope_id:
            label = f'{visibility}/{scope_id}'
        else:
            label = str(visibility)
        bucket = out.setdefault(label, {'live': 0, 'deleted': 0, 'shadowed': 0})
        bucket['live'] += int(live or 0)
        bucket['deleted'] += int(deleted or 0)
        bucket['shadowed'] += int(shadowed or 0)
    return out


@dataclass(frozen=True)
class ScopeInventory:
    declared: tuple[str, ...]
    zenoh: dict[str, Counter[str]]
    sqlite: dict[str, dict[str, int]]

    @property
    def undeclared(self) -> tuple[str, ...]:
        """Scope labels present locally that this host no longer declares.

        ``legacy`` and ``other`` are excluded: legacy keys belong to
        ``migrate-visibility``, and ``other`` is not a scope to purge.
        """
        seen = set(self.zenoh) | set(self.sqlite)
        return tuple(sorted(s for s in seen if s not in self.declared and s not in ('legacy', 'other')))


def scope_inventory(session: Any, *, db_path: str | Path, self_only: bool = True) -> ScopeInventory:
    """Read-only report: Zenoh directory probe plus SQLite index counts."""
    return ScopeInventory(
        declared=tuple(s.label for s in scope_mod.resolve_storage_scopes()),
        zenoh=probe_inventory(session, self_only=self_only),
        sqlite=payload_scope_states(db_path),
    )


# -- host-local purge ----------------------------------------------------------


@dataclass(frozen=True)
class PurgePlan:
    declared: tuple[str, ...]
    rows: dict[str, list[str]]
    dirs: tuple[Path, ...]

    @property
    def row_count(self) -> int:
        return sum(len(ids) for ids in self.rows.values())

    @property
    def empty(self) -> bool:
        return not self.rows and not self.dirs


def _undeclared_rows(db_path: Path, declared: set[str]) -> dict[str, list[str]]:
    if not db_path.exists():
        return {}
    try:
        con = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    except sqlite3.Error as e:
        log.warning('build_purge_plan: cannot open %s read-only: %s', db_path, e)
        return {}
    try:
        rows = con.execute(
            "SELECT observation_id, COALESCE(json_extract(payload_json, '$.visibility'), ''), "
            "COALESCE(json_extract(payload_json, '$.scope_id'), '') FROM obs_index"
        ).fetchall()
    except sqlite3.Error as e:
        log.warning('build_purge_plan: query failed on %s: %s', db_path, e)
        return {}
    finally:
        con.close()
    out: dict[str, list[str]] = {}
    for obs_id, visibility, scope_id in rows:
        if not visibility:
            continue  # legacy rows are migrate-visibility's business, not purge's
        label = f'{visibility}/{scope_id}' if scope_id else str(visibility)
        if label in declared:
            continue
        out.setdefault(label, []).append(str(obs_id))
    return {label: sorted(ids) for label, ids in sorted(out.items())}


def build_purge_plan(
    *,
    db_path: str | Path,
    rocksdb_root: str | Path,
    session: Any | None = None,
    declared: tuple[str, ...] | None = None,
) -> PurgePlan:
    """Plan the host-local removal of every scope this host does not declare.

    Two kinds of leftover, both host-local: ``obs_index`` rows and whole
    RocksDB directories. A directory that a live storage is currently serving
    is left out — RocksDB is single-writer, and a served directory means the
    host still declares that scope somewhere.

    The pre-split ``agent_mem`` directory is never a target: it is the
    cutover's rollback artifact, and when it may go is the runbook's decision.
    """
    labels = declared if declared is not None else tuple(s.label for s in scope_mod.resolve_storage_scopes())
    declared_set = set(labels)
    rows = _undeclared_rows(Path(db_path), declared_set)

    served, served_known = _served_dirs(session)
    root = Path(rocksdb_root)
    dirs: list[Path] = []
    if served_known and root.is_dir():
        for candidate in sorted(root.iterdir()):
            if not candidate.is_dir() or candidate.name in served or candidate.name == LEGACY_SOURCE_DIR:
                continue
            if _label_for_volume_dir(candidate.name, declared_set) is not None:
                dirs.append(candidate)
    return PurgePlan(declared=labels, rows=rows, dirs=tuple(dirs))


def _served_dirs(session: Any | None) -> tuple[set[str], bool]:
    """Directories the live zenohd serves, and whether that could be determined.

    Unknown means no directory is a purge candidate: purging one that a running
    storage still writes to would corrupt it, and "the admin space did not
    answer" is not evidence that nothing is served.
    """
    if session is None:
        return set(), False
    try:
        return {s.volume_dir for s in scope_mod.fetch_self_storages(session)}, True
    except Exception as e:  # noqa: BLE001 — no admin space: keep every directory
        log.warning('build_purge_plan: cannot read live storages (%s); no directory will be purged', e)
        return set(), False


def _label_for_volume_dir(name: str, declared: set[str]) -> str | None:
    """Return the scope label whose directory is ``name``, if it is undeclared.

    Only directory names that a scope would produce are candidates, so an
    unrelated directory under the RocksDB root is never a purge target.
    """
    if name == ScopeSpec('mesh').volume_dir:
        label = 'mesh'
    elif '_' in name:
        tier, _, slug = name.partition('_')
        label = f'{tier}/{slug}'
    else:
        return None
    try:
        spec = scope_mod.parse_scope(label)
    except scope_mod.ScopeConfigError:
        return None
    if spec.volume_dir != name or spec.label in declared:
        return None
    return spec.label


@dataclass
class PurgeResult:
    rows_deleted: int = 0
    dirs_renamed: tuple[tuple[Path, Path], ...] = ()


def execute_purge(plan: PurgePlan, *, index: Any, now_stamp: str) -> PurgeResult:
    """Apply a purge plan: delete index rows, rename directories aside.

    No Zenoh delete, ever (see the module docstring). Directories are renamed
    to ``<dir>.purged-<stamp>`` rather than removed, so a mistake is
    recoverable with a ``mv`` instead of a restore from backup.
    """
    deleted = 0
    for ids in plan.rows.values():
        for obs_id in ids:
            index.physical_delete(obs_id)
            deleted += 1
    renamed: list[tuple[Path, Path]] = []
    for directory in plan.dirs:
        target = directory.with_name(f'{directory.name}.purged-{now_stamp}')
        os.replace(directory, target)
        renamed.append((directory, target))
    return PurgeResult(rows_deleted=deleted, dirs_renamed=tuple(renamed))
