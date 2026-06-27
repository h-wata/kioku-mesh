"""Tests for ADR-0027 importance-aware search ranking.

Importance influences ordering only when a query expresses intent:
  - FTS path: importance is the primary key, bm25 relevance breaks ties
    within an importance level.
  - LIKE / short-query path (no bm25 score): importance primary, then recency.
  - query-less browse and cursor pagination are intentionally untouched.

Pure SQLite tests — no zenohd.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kioku_mesh.core.models import Observation
from kioku_mesh.memory.local_index import _FTS_CAP_LIKE
from kioku_mesh.memory.local_index import LocalIndex


def _skip_if_no_fts(index: LocalIndex) -> None:
    """Skip a test that exercises FTS-path bm25 tiebreaking when only LIKE exists."""
    if index._fts_cap == _FTS_CAP_LIKE:  # noqa: SLF001
        pytest.skip('test exercises bm25 tiebreak on the FTS path; LIKE fallback has no bm25 score')


def _obs(
    content: str,
    *,
    importance: int,
    created_at: str,
    subject: str = '',
    project: str = 'demo',
    memory_type: str = 'decision',
) -> Observation:
    return Observation(
        content=content,
        project=project,
        memory_type=memory_type,
        subject=subject,
        importance=importance,
        created_at=created_at,
    )


@pytest.fixture
def idx(tmp_path: Path) -> LocalIndex:
    index = LocalIndex.connect(str(tmp_path / 'index.db'))
    yield index
    index.close()


def test_importance_breaks_tie_over_recency_on_query(idx: LocalIndex) -> None:
    """With equal relevance, higher importance outranks a *newer* low one."""
    # Identical content → identical bm25 rank, so importance is the deciding
    # key. The high-importance row is OLDER, so a win proves importance beats
    # recency (not just that newer sorts first).
    idx.upsert(_obs('billing decision alpha', importance=5, created_at='2026-01-01T00:00:00.000000Z'))
    idx.upsert(_obs('billing decision alpha', importance=2, created_at='2026-02-01T00:00:00.000000Z'))

    hits = idx.search(query='billing')
    assert len(hits) == 2
    assert hits[0].importance == 5, 'high-importance older entry should rank first'


def test_importance_lifts_comparable_match(idx: LocalIndex) -> None:
    """Important decision beats a trivial note of comparable relevance.

    bm25 alone would rank the short note first; importance-primary fixes it.
    """
    _skip_if_no_fts(idx)
    idx.upsert(
        _obs(
            'billing trivial aside',
            importance=2,
            memory_type='note',
            subject='billing',
            created_at='2026-01-01T00:00:00.000000Z',
        )
    )
    idx.upsert(
        _obs(
            'billing events are append-only — core decision',
            importance=5,
            memory_type='decision',
            subject='billing',
            created_at='2026-01-02T00:00:00.000000Z',
        )
    )

    hits = idx.search(query='billing')
    assert hits[0].importance == 5, 'important decision should outrank a comparably-relevant trivial note'


def test_bm25_orders_within_same_importance(idx: LocalIndex) -> None:
    """Within one importance level, bm25 relevance still decides order.

    importance is the primary key, but the relevance signal must remain the
    tiebreak so equally-important matches are not ordered arbitrarily.
    """
    _skip_if_no_fts(idx)
    # Same importance; the first is a tighter match for the multi-term query.
    idx.upsert(
        _obs(
            'billing invoice retention policy',
            importance=3,
            subject='billing',
            created_at='2026-01-01T00:00:00.000000Z',
        )
    )
    idx.upsert(
        _obs(
            'billing only, no other matching terms here',
            importance=3,
            subject='billing',
            created_at='2026-01-02T00:00:00.000000Z',
        )
    )

    hits = idx.search(query='billing invoice retention')
    assert hits[0].content.startswith('billing invoice retention'), 'bm25 should order within equal importance'


def test_importance_primary_on_short_query_like_path(idx: LocalIndex) -> None:
    """A 2-char query (no FTS) ranks by importance first, then recency."""
    # 'xx' is < 3 chars so it takes the LIKE path (no bm25 score available).
    idx.upsert(_obs('xx value', importance=5, created_at='2026-01-01T00:00:00.000000Z'))
    idx.upsert(_obs('xx value', importance=2, created_at='2026-02-01T00:00:00.000000Z'))

    hits = idx.search(query='xx')
    assert [h.importance for h in hits] == [5, 2]


def test_browse_without_query_stays_chronological(idx: LocalIndex) -> None:
    """No query → recency order; importance must NOT reorder a browse listing."""
    idx.upsert(_obs('alpha', importance=5, created_at='2026-01-01T00:00:00.000000Z'))
    idx.upsert(_obs('beta', importance=2, created_at='2026-02-01T00:00:00.000000Z'))

    hits = idx.search()  # no query
    # Newest first regardless of importance.
    assert [h.importance for h in hits] == [2, 5]


def test_importance_ranking_in_or_mode(idx: LocalIndex) -> None:
    """OR-mode query also ranks by importance first."""
    idx.upsert(_obs('billing decision', importance=5, created_at='2026-01-01T00:00:00.000000Z'))
    idx.upsert(_obs('billing decision', importance=1, created_at='2026-02-01T00:00:00.000000Z'))

    hits = idx.search(query='billing', search_mode='or')
    assert hits[0].importance == 5


def test_cursor_pagination_ignores_importance(idx: LocalIndex) -> None:
    """Cursor pagination must keep (created_at, observation_id) order intact.

    With a query AND a cursor set, importance must not enter the ORDER BY or
    the strict-tuple walk would skip/repeat rows. We assert the page is
    ordered strictly by created_at DESC, not by importance.
    """
    idx.upsert(_obs('billing a', importance=1, created_at='2026-01-03T00:00:00.000000Z'))
    idx.upsert(_obs('billing b', importance=5, created_at='2026-01-02T00:00:00.000000Z'))
    idx.upsert(_obs('billing c', importance=3, created_at='2026-01-01T00:00:00.000000Z'))

    hits = idx.search(
        query='billing',
        until_iso='2026-01-04T00:00:00.000000Z',
        cursor_observation_id='ffffffffffffffffffffffffffffffff',
    )
    created = [h.created_at for h in hits]
    assert created == sorted(created, reverse=True), 'cursor page must stay in created_at DESC order'
