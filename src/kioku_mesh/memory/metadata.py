"""Required-metadata rules for kioku-mesh observations.

``subject`` and ``summary`` are what search / recall render before falling
back to the full body, so an entry missing them costs every future reader a
full-content read. ADR-0028 Phase5 shipped these as *warn-only* lint
(:mod:`kioku_mesh.memory.save_lint`) and the warnings were ignored in
practice — roughly a quarter of stored observations have neither. This
module is the enforcing counterpart: write entry points (CLI ``save``, MCP
``save_observation``) reject a save that omits them.

Deliberately NOT enforced on ingest paths (replication subscriber, index
rebuild, ``Observation.from_json``): a peer on an older version must still be
readable, and rejecting its payloads would silently drop mesh data.

The same "is this actually filled in?" predicate is reused by
``kioku-mesh backfill-metadata`` so enforcement and repair agree on what
counts as missing.
"""

from __future__ import annotations

import re

# Values observed in production standing in for "I have nothing to say here".
# Compared case-insensitively after stripping surrounding whitespace and
# punctuation-only content, so '-', '--', '...' and 'N/A' all count as absent.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset(
    {
        '-',
        '--',
        '---',
        '.',
        '..',
        '...',
        'n/a',
        'na',
        'none',
        'null',
        'nil',
        'tbd',
        'todo',
        '未定',
        'なし',
    }
)

_SENTENCE_SPLIT = re.compile(r'(?<=[。．.!?！？])\s*')

SUBJECT_MAX = 80
SUMMARY_MAX = 200


class MetadataRequiredError(ValueError):
    """Raised when a write entry point is given an observation without usable metadata."""


def is_missing(value: str | None) -> bool:
    """Return True when ``value`` carries no information.

    Empty, whitespace-only, and known placeholder strings (``'-'``, ``'N/A'``,
    ``'TBD'``, …) are all treated as missing — storing them is
    indistinguishable, for a reader, from storing nothing.
    """
    if value is None:
        return True
    cleaned = value.strip()
    if not cleaned:
        return True
    return cleaned.lower() in _PLACEHOLDER_VALUES


def validate_required_metadata(subject: str | None, summary: str | None) -> None:
    """Raise :class:`MetadataRequiredError` unless both fields are filled in.

    Both are reported in a single message so a caller that omitted both does
    not have to round-trip twice to learn the second requirement.
    """
    missing = [name for name, value in (('subject', subject), ('summary', summary)) if is_missing(value)]
    if not missing:
        return
    raise MetadataRequiredError(
        f'{" and ".join(missing)} required: '
        'subject is the short topic (e.g. "recall_context latency"), '
        'summary is the one-line abstract shown in search results. '
        "Empty, whitespace-only and placeholder values ('-', 'N/A', 'TBD') do not count — "
        'write what a future searcher would actually look for.'
    )


def derive_subject(content: str, limit: int = SUBJECT_MAX) -> str:
    """Best-effort subject from ``content``: its first non-empty line, truncated.

    Used only by ``backfill-metadata`` to repair entries already stored
    without a subject. Never used on the write path — a derived subject is
    strictly worse than one the author would have written, so new saves are
    rejected instead of being papered over.
    """
    for line in content.splitlines():
        stripped = line.strip().lstrip('#').strip()
        if stripped:
            return _truncate(stripped, limit)
    return ''


def derive_summary(content: str, limit: int = SUMMARY_MAX) -> str:
    """Best-effort summary from ``content``: its first sentence, truncated.

    Same backfill-only caveat as :func:`derive_subject`.
    """
    flattened = ' '.join(content.split())
    if not flattened:
        return ''
    first = _SENTENCE_SPLIT.split(flattened, maxsplit=1)[0].strip()
    return _truncate(first or flattened, limit)


def _truncate(value: str, limit: int) -> str:
    """Return ``value`` cut to ``limit`` characters with an ellipsis marker."""
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + '…'
