"""Render zenohd storages from ``storage_scopes`` (ADR-0019 Phase E, design v3 task 3).

One storage per declared scope, derived from the shared contract in
:mod:`kioku_mesh.core.scope` — this module never re-derives key expressions,
strip prefixes or volume dirs itself, so the renderer, the save preflight and
doctor cannot drift apart.

Two properties of the emitted config carry the design's B1 decision:

- the ``mesh`` storage uses a **new, empty** RocksDB directory (``mesh``), not
  the pre-split ``agent_mem``. Zenoh replication alignment distributes keys that
  are already in a directory even when they fall outside the storage's
  ``key_expr``, so reusing ``agent_mem`` would hand legacy / user / team keys to
  every peer of the mesh replica group.
- ``strip_prefix`` for ``mesh`` stays ``mem``: existing ``mem/mesh/...`` keys are
  stored as ``mesh/...`` on disk. ``mem/mesh`` would rewrite them to
  ``mem/mesh/mesh/...``.

**A half-applied cutover does not heal itself.** Storages with different
``key_expr`` values form different replica groups, so a host left on the old
broad ``agent_mem`` config cannot align with the new ``mem/mesh/**`` group: it
still receives live publications, but whatever it misses while it lags is never
backfilled. Apply the rendered config on every peer within one maintenance
window and do not resume normal operation on a partially converted mesh.

The rewrite is textual and surgical on purpose: only the ``storages`` block is
replaced, so listen / connect / transport TLS / timestamping / any hand-added
setting in an existing ``zenohd.json5`` survives byte-for-byte. Regenerating the
whole file (``init --force``) would silently drop them.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import sqlite3

from .scope import ScopeSpec

# Must match byte-for-byte across all peers of a replica group.
REPLICATION_FIELDS: tuple[tuple[str, str], ...] = (
    ('interval', '10.0'),
    ('sub_intervals', '5'),
    ('hot', '6'),
    ('warm', '30'),
    ('propagation_delay', '250'),
)

# Pre-split broad storage, kept read-only during the transitional window so the
# migration coordinator can query ``mem/mesh/**`` out of it (design v3 step 3).
LEGACY_SOURCE_NAME = 'legacy_source_store'
LEGACY_SOURCE_KEY_EXPR = 'mem/**'
LEGACY_SOURCE_DIR = 'agent_mem'

_STORAGE_INDENT = ' ' * 8


class StorageRenderError(ValueError):
    """The target config has no storages block this renderer can replace."""


def _render_entry(name: str, key_expr: str, strip_prefix: str, volume_dir: str, indent: str = _STORAGE_INDENT) -> str:
    inner = indent + '  '
    lines = [
        f'{indent}{name}: {{',
        f'{inner}key_expr: "{key_expr}",',
        f'{inner}strip_prefix: "{strip_prefix}",',
        f'{inner}replication: {{',
    ]
    lines += [f'{inner}  {field}: {value},' for field, value in REPLICATION_FIELDS]
    lines += [
        f'{inner}}},',
        f'{inner}volume: {{',
        f'{inner}  id: "rocksdb",',
        f'{inner}  dir: "{volume_dir}",',
        f'{inner}  create_db: true,',
        f'{inner}}},',
        f'{indent}}},',
    ]
    return '\n'.join(lines)


def render_storage_entries(
    scopes: tuple[ScopeSpec, ...] | list[ScopeSpec],
    *,
    include_legacy_source: bool = False,
    indent: str = _STORAGE_INDENT,
) -> str:
    """Render the body of the ``storages`` block, one entry per scope.

    ``include_legacy_source`` adds the transitional read-only ``agent_mem``
    storage alongside the new scope storages. It belongs only to the migration
    window: the final config drops it, and running with both long-term keeps a
    broad storage that the save preflight refuses to write through.
    """
    entries = [_render_entry(s.storage_name, s.key_expr, s.strip_prefix, s.volume_dir, indent) for s in scopes]
    if include_legacy_source:
        entries.insert(0, _render_entry(LEGACY_SOURCE_NAME, LEGACY_SOURCE_KEY_EXPR, 'mem', LEGACY_SOURCE_DIR, indent))
    return '\n'.join(entries)


def _block_span(text: str, key: str) -> tuple[int, int, str]:
    """Return ``(inner_start, closing_brace_index, indent)`` of ``key: { ... }``.

    Brace counting skips string literals and ``//`` comments so a brace inside
    a certificate path or a comment cannot end the block early.
    """
    m = re.search(rf'^([ \t]*){re.escape(key)}\s*:\s*\{{', text, re.M)
    if m is None:
        raise StorageRenderError(f'no `{key}:` block found')
    i = text.index('{', m.start())
    depth = 0
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == '\\':
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif text.startswith('//', i):
            i = text.find('\n', i)
            if i == -1:
                break
            continue
        elif ch == '{':
            depth += 1
            if depth == 1:
                inner_start = i + 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return inner_start, i, m.group(1)
        i += 1
    raise StorageRenderError(f'unterminated `{key}:` block')


def replace_storages(config_text: str, entries: str) -> str:
    """Return ``config_text`` with its ``storages`` block body replaced.

    Everything else — endpoints, transport TLS, timestamping, comments, unknown
    keys — is preserved exactly, which is what makes this safe to run against a
    config that was hand-edited after ``init``.
    """
    inner_start, closing, indent = _block_span(config_text, 'storages')
    return f'{config_text[:inner_start]}\n{entries}\n{indent}{config_text[closing:]}'


def payload_scope_counts(db_path: str | Path) -> Counter[str]:
    """Count observations per scope label in an existing SQLite index.

    Read-only (``mode=ro``) and best-effort: a missing or unreadable database
    returns an empty counter rather than blocking a render. Counts every row —
    live, deleted and shadowed alike — because a scope with only deleted rows
    still has keys in the mesh that need a storage to land in. Legacy
    (pre-visibility) rows are reported under ``legacy``.
    """
    path = Path(db_path)
    if not path.exists():
        return Counter()
    try:
        con = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    except sqlite3.Error:
        return Counter()
    try:
        rows = con.execute(
            "SELECT COALESCE(json_extract(payload_json, '$.visibility'), ''), "
            "COALESCE(json_extract(payload_json, '$.scope_id'), ''), COUNT(*) "
            'FROM obs_index GROUP BY 1, 2'
        ).fetchall()
    except sqlite3.Error:
        return Counter()
    finally:
        con.close()
    counts: Counter[str] = Counter()
    for visibility, scope_id, count in rows:
        if not visibility:
            counts['legacy'] += count
        elif scope_id:
            counts[f'{visibility}/{scope_id}'] += count
        else:
            counts[visibility] += count
    return counts


def missing_scope_counts(scopes: tuple[ScopeSpec, ...] | list[ScopeSpec], db_path: str | Path) -> Counter[str]:
    """Return payload scopes that no declared storage would hold.

    The upgrade case this exists for: a host with no ``storage_scopes`` renders
    ``[mesh]``, and a user/team scope it has been storing all along would
    quietly lose its storage. ``legacy`` rows are excluded — they are the
    ``migrate-visibility`` gate's business, not the renderer's.
    """
    declared = {s.label for s in scopes}
    counts = payload_scope_counts(db_path)
    return Counter({label: n for label, n in counts.items() if label != 'legacy' and label not in declared})
