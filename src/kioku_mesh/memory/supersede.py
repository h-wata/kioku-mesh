"""Supersede-candidate detection (ADR-0026).

The append-only model (ADR-0002) represents an "update" as a fresh
observation. ADR-0021 added the *representation* and *read* side of
superseding (the ``superseded_by`` index column, existence-based hiding,
rebuild reconstruction), but the *trigger* stayed fully manual: a caller
must pass ``supersedes=[old_id]`` when saving a revised entry. Forgetting
to do so leaves the stale and the current entry both live in search — the
"hallucinations of the past" failure mode that arXiv:2606.24775 flags as
the central risk of append-only memory.

This module adds only the **detection** half of the fix (suggest-first,
ADR-0026 §A): given an observation about to be (or just) saved, find the
live entries it most plausibly supersedes, so the CLI / MCP layer can
surface them. It performs no writes and changes no replication semantics —
the caller decides whether to act on the suggestion.

Scope is deliberately narrow: only the revisable ``decision`` / ``config``
types, only same project + memory_type + normalized subject + replication
scope. ``subject`` is a free-text, *weak* key, which is exactly why the
default behavior is to suggest rather than silently supersede.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.models import Observation
    from .local_index import LocalIndex

# Memory types whose updates are revisions of the same fact, not additive
# notes. ``note`` / ``bug`` / ``pattern`` / ``summary`` accumulate naturally
# and are intentionally excluded.
SUPERSEDE_TYPES: frozenset[str] = frozenset({'decision', 'config'})

# Cap how many candidates we surface. A handful is enough to act on; more
# usually means the subject is too coarse to be a useful key.
DEFAULT_CANDIDATE_LIMIT = 5

# Bound the live-row pool we scan per project so a huge project cannot make
# a save (or a doctor sweep) pay an unbounded parse cost. Decisions per
# project are few in practice; this is a backstop, not a tuning knob.
_POOL_LIMIT = 10_000


def normalize_subject(subject: str) -> str:
    """Return ``subject`` casefolded with collapsed surrounding whitespace.

    Intentionally light: lowercases (casefold for non-ASCII) and collapses
    runs of whitespace. It does NOT stem, transliterate, or resolve
    synonyms — ``subject`` remains a weak key, so detection favors precision
    (exact normalized match) over recall and the caller keeps the final say.
    """
    return ' '.join(subject.casefold().split())


def find_candidates_in_index(
    idx: 'LocalIndex',
    obs: 'Observation',
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list['Observation']:
    """Return live observations ``obs`` most plausibly supersedes.

    A candidate must share ``obs``'s ``project``, ``memory_type`` (one of
    :data:`SUPERSEDE_TYPES`), normalized ``subject``, and replication scope
    (``visibility`` + ``scope_id``), and must itself be live (not
    tombstoned, shadowed, or already superseded). ``obs`` is excluded from
    its own candidate list. Results are ordered most-recent-first (the
    ``LocalIndex.search`` default) and capped at ``limit``.

    Returns an empty list for non-revisable types, an empty subject, or a
    disabled index — detection is best-effort and never raises into the
    save path.
    """
    subject_key = normalize_subject(obs.subject)
    if obs.memory_type not in SUPERSEDE_TYPES or not subject_key:
        return []

    # Narrow at the SQL layer by project + memory_type (both real columns);
    # the normalized-subject and scope match is done in Python because
    # ``subject`` is stored as free text and scope lives in payload_json.
    pool = idx.search(
        project=obs.project,
        memory_type=obs.memory_type,
        include_superseded=False,
        limit=_POOL_LIMIT,
    )

    out: list['Observation'] = []
    for cand in pool:
        if cand.observation_id == obs.observation_id:
            continue
        if normalize_subject(cand.subject) != subject_key:
            continue
        # Only suggest within the same replication scope: a team-scoped
        # decision should not be flagged as superseding a user-scoped one.
        if cand.visibility != obs.visibility or cand.scope_id != obs.scope_id:
            continue
        out.append(cand)
        if len(out) >= limit:
            break
    return out
