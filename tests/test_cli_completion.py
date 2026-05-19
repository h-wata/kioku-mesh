"""Unit tests for CLI shell-completion plumbing (Issue #76).

Covers:
    - ``LocalIndex.distinct_projects`` / ``distinct_pc_ids`` return the
      expected unique values from a populated index.
    - ``_complete_project`` / ``_complete_pc_id`` filter by the typed prefix
      and degrade silently to ``[]`` on local-index failure.
    - Completers do not initialise Zenoh (no ``get_session`` call) — the
      completion subshell must stay fast.

Tests stay at the pure-SQLite layer; no zenohd fixture required.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import mesh_mem.__main__ as cli_module
from mesh_mem.local_index import LocalIndex
from mesh_mem.models import Observation


def _mk_obs(*, project: str, pc_id: str, content: str = 'x') -> Observation:
    return Observation(
        content=content,
        project=project,
        agent_family='claude',
        client_id='test',
        pc_id=pc_id,
        session_id='sess',
    )


def _populate(db_path: Path) -> None:
    """Upsert a fixed-shape set of observations covering 3 projects / 2 pc_ids."""
    idx = LocalIndex.connect(str(db_path))
    try:
        idx.upsert(_mk_obs(project='alpha', pc_id='pc-aaa'))
        idx.upsert(_mk_obs(project='alpha', pc_id='pc-aaa'))  # dup project + pc_id
        idx.upsert(_mk_obs(project='beta', pc_id='pc-bbb'))
        idx.upsert(_mk_obs(project='gamma', pc_id='pc-aaa'))
        # blank project must be filtered out — completion should never offer ''
        idx.upsert(_mk_obs(project='', pc_id='pc-ccc'))
    finally:
        idx.close()


def test_distinct_projects_returns_unique_non_empty(tmp_path: Path) -> None:
    _populate(tmp_path / 'index.db')
    idx = LocalIndex.connect(str(tmp_path / 'index.db'))
    try:
        assert idx.distinct_projects() == ['alpha', 'beta', 'gamma']
    finally:
        idx.close()


def test_distinct_pc_ids_returns_unique(tmp_path: Path) -> None:
    _populate(tmp_path / 'index.db')
    idx = LocalIndex.connect(str(tmp_path / 'index.db'))
    try:
        # Order is sorted; blank pc_id is impossible (identity ensures one)
        # but ``pc-ccc`` from the blank-project row is still a real pc_id.
        assert idx.distinct_pc_ids() == ['pc-aaa', 'pc-bbb', 'pc-ccc']
    finally:
        idx.close()


def test_distinct_projects_disabled_returns_empty(tmp_path: Path) -> None:
    """A disabled index must return ``[]`` instead of raising — completers depend on this."""
    idx = LocalIndex(db_path='', disabled=True)
    assert idx.distinct_projects() == []
    assert idx.distinct_pc_ids() == []


def test_complete_project_filters_by_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / 'index.db'
    _populate(db)
    monkeypatch.setenv('MESH_MEM_INDEX_DB', str(db))

    assert cli_module._complete_project(prefix='') == ['alpha', 'beta', 'gamma']
    assert cli_module._complete_project(prefix='a') == ['alpha']
    assert cli_module._complete_project(prefix='gam') == ['gamma']
    assert cli_module._complete_project(prefix='zzz') == []


def test_complete_pc_id_filters_by_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / 'index.db'
    _populate(db)
    monkeypatch.setenv('MESH_MEM_INDEX_DB', str(db))

    assert cli_module._complete_pc_id(prefix='pc-') == ['pc-aaa', 'pc-bbb', 'pc-ccc']
    assert cli_module._complete_pc_id(prefix='pc-a') == ['pc-aaa']
    assert cli_module._complete_pc_id(prefix='nope') == []


def test_completers_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``LocalIndex.connect`` to raise — completers must still return ``[]``."""

    def _boom(*_a, **_kw) -> 'LocalIndex':
        raise RuntimeError('disk on fire')

    monkeypatch.setattr(cli_module.LocalIndex, 'connect', staticmethod(_boom))
    assert cli_module._complete_project(prefix='') == []
    assert cli_module._complete_pc_id(prefix='') == []


def test_completers_do_not_touch_zenoh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Completion runs in a shell subshell — must not call ``get_session``.

    If a future refactor accidentally pulls the Zenoh session into the
    completer path, this assertion fails and the regression is caught
    before users see 15s tab-completion latency.
    """
    db = tmp_path / 'index.db'
    _populate(db)
    monkeypatch.setenv('MESH_MEM_INDEX_DB', str(db))

    with mock.patch('mesh_mem.store.get_session') as get_session:
        cli_module._complete_project(prefix='')
        cli_module._complete_pc_id(prefix='')
        get_session.assert_not_called()


def test_attach_completer_no_op_without_argcomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    """When argcomplete is not installed, ``_attach_completer`` must be a silent no-op."""
    monkeypatch.setattr(cli_module, 'argcomplete', None)

    # Build a throwaway parser so we have a real Action object.
    import argparse

    parser = argparse.ArgumentParser()
    action = parser.add_argument('--project', default='')
    cli_module._attach_completer(action, cli_module._complete_project)
    # No completer attribute is set when argcomplete is absent — argcomplete's
    # contract is "any unspecified action falls back to default completion".
    assert not hasattr(action, 'completer')
