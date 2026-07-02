"""Unit tests for the namespace-aware key vocabulary (ADR-0019 Phase A)."""

from __future__ import annotations

import threading
import time

import pytest
import zenoh

from kioku_mesh import keyspace
from kioku_mesh.core import config as cfg_mod

_ID = 'a' * 32


def test_obs_id_from_key_accepts_all_namespaces() -> None:
    """Legacy, mesh, user and team shapes all parse to the trailing id."""
    assert keyspace.obs_id_from_key(f'mem/obs/fam/cli/pc/sess/{_ID}') == _ID
    assert keyspace.obs_id_from_key(f'mem/tomb/fam/cli/pc/sess/{_ID}') == _ID
    assert keyspace.obs_id_from_key(f'mem/mesh/obs/fam/cli/pc/sess/{_ID}') == _ID
    assert keyspace.obs_id_from_key(f'mem/mesh/tomb/fam/cli/pc/sess/{_ID}') == _ID
    assert keyspace.obs_id_from_key(f'mem/user/hwata/obs/fam/cli/pc/sess/{_ID}') == _ID
    assert keyspace.obs_id_from_key(f'mem/user/hwata/tomb/fam/cli/pc/sess/{_ID}') == _ID
    assert keyspace.obs_id_from_key(f'mem/team/kioku-mesh/obs/fam/cli/pc/sess/{_ID}') == _ID
    assert keyspace.obs_id_from_key(f'mem/team/kioku-mesh/tomb/fam/cli/pc/sess/{_ID}') == _ID


def test_obs_id_from_key_rejects_malformed_keys() -> None:
    """The parser stays conservative (#64): anything off-shape is None."""
    # Bad ids
    assert keyspace.obs_id_from_key('mem/obs/fam/cli/pc/sess/' + 'A' * 32) is None
    assert keyspace.obs_id_from_key('mem/obs/fam/cli/pc/sess/short') is None
    assert keyspace.obs_id_from_key('mem/obs/fam/cli/pc/sess/' + 'g' * 32) is None
    # Wrong namespaces / prefixes
    assert keyspace.obs_id_from_key(f'other/ns/fam/cli/pc/sess/{_ID}') is None
    assert keyspace.obs_id_from_key(f'mem/control/fam/cli/pc/sess/{_ID}') is None
    assert keyspace.obs_id_from_key(f'/mem/obs/fam/cli/pc/sess/{_ID}') is None
    # Wrong segment counts per namespace
    assert keyspace.obs_id_from_key(f'mem/obs/fam/cli/{_ID}') is None
    assert keyspace.obs_id_from_key(f'mem/obs/fam/cli/pc/sess/extra/{_ID}') is None
    assert keyspace.obs_id_from_key(f'mem/mesh/obs/fam/cli/pc/sess/extra/{_ID}') is None
    assert keyspace.obs_id_from_key(f'mem/user/obs/fam/cli/pc/sess/{_ID}') is None  # missing scope id
    assert keyspace.obs_id_from_key(f'mem/user//obs/fam/cli/pc/sess/{_ID}') is None  # empty scope id
    assert keyspace.obs_id_from_key(f'mem/team/x/y/obs/fam/cli/pc/sess/{_ID}') is None
    # Marker missing where the shape demands it
    assert keyspace.obs_id_from_key(f'mem/mesh/fam/cli/pc/sess/{_ID}') is None
    assert keyspace.obs_id_from_key(f'mem/user/hwata/fam/cli/pc/sess/{_ID}') is None


def test_read_selectors_cover_all_namespaces() -> None:
    """The broadened selectors intersect every namespace shape — and only the right kind."""
    obs_ke = zenoh.KeyExpr(keyspace.OBS_READ_KEY_EXPR)
    tomb_ke = zenoh.KeyExpr(keyspace.TOMB_READ_KEY_EXPR)
    obs_keys = [
        f'mem/obs/fam/cli/pc/sess/{_ID}',
        f'mem/mesh/obs/fam/cli/pc/sess/{_ID}',
        f'mem/user/hwata/obs/fam/cli/pc/sess/{_ID}',
        f'mem/team/kioku-mesh/obs/fam/cli/pc/sess/{_ID}',
    ]
    for k in obs_keys:
        assert obs_ke.intersects(zenoh.KeyExpr(k)), k
        assert not tomb_ke.intersects(zenoh.KeyExpr(k)), k
        tomb_k = k.replace('/obs/', '/tomb/', 1)
        assert tomb_ke.intersects(zenoh.KeyExpr(tomb_k)), tomb_k
        assert not obs_ke.intersects(zenoh.KeyExpr(tomb_k)), tomb_k


def test_identity_scoped_selectors_cover_all_namespaces() -> None:
    """Identity narrowing applies positionally after the obs marker in every namespace."""
    sel = zenoh.KeyExpr(keyspace.obs_selector(agent_family='claude'))
    assert sel.intersects(zenoh.KeyExpr(f'mem/obs/claude/cli/pc/sess/{_ID}'))
    assert sel.intersects(zenoh.KeyExpr(f'mem/user/hwata/obs/claude/cli/pc/sess/{_ID}'))
    assert not sel.intersects(zenoh.KeyExpr(f'mem/obs/gemini/cli/pc/sess/{_ID}'))

    tomb_sel = zenoh.KeyExpr(keyspace.tomb_selector(agent_family='claude'))
    assert tomb_sel.intersects(zenoh.KeyExpr(f'mem/mesh/tomb/claude/cli/pc/sess/{_ID}'))
    assert not tomb_sel.intersects(zenoh.KeyExpr(f'mem/mesh/obs/claude/cli/pc/sess/{_ID}'))


def test_find_by_id_selector_covers_all_namespaces() -> None:
    sel = zenoh.KeyExpr(keyspace.find_by_id_selector(_ID))
    assert sel.intersects(zenoh.KeyExpr(f'mem/obs/f/c/p/s/{_ID}'))
    assert sel.intersects(zenoh.KeyExpr(f'mem/team/x/obs/f/c/p/s/{_ID}'))
    assert not sel.intersects(zenoh.KeyExpr(f'mem/obs/f/c/p/s/{"b" * 32}'))


def test_obs_key_builders_per_tier() -> None:
    """Phase B: writers branch on visibility; '' keeps the legacy layout."""
    args = ('claude', 'cc', 'pc1', 's1', _ID)
    assert keyspace.obs_key('', '', *args) == f'mem/obs/claude/cc/pc1/s1/{_ID}'
    assert keyspace.obs_key('mesh', '', *args) == f'mem/mesh/obs/claude/cc/pc1/s1/{_ID}'
    assert keyspace.obs_key('user', 'hwata', *args) == f'mem/user/hwata/obs/claude/cc/pc1/s1/{_ID}'
    assert keyspace.obs_key('team', 'kioku', *args) == f'mem/team/kioku/obs/claude/cc/pc1/s1/{_ID}'
    assert keyspace.tomb_key('user', 'hwata', *args) == f'mem/user/hwata/tomb/claude/cc/pc1/s1/{_ID}'
    # Every built key round-trips through the canonical parser.
    for vis, scope in [('', ''), ('mesh', ''), ('user', 'hwata'), ('team', 'kioku')]:
        assert keyspace.obs_id_from_key(keyspace.obs_key(vis, scope, *args)) == _ID
        assert keyspace.obs_id_from_key(keyspace.tomb_key(vis, scope, *args)) == _ID


def test_obs_key_builders_reject_bad_input() -> None:
    import pytest

    args = ('claude', 'cc', 'pc1', 's1', _ID)
    with pytest.raises(ValueError):
        keyspace.obs_key('user', '', *args)  # scoped tier without scope id
    with pytest.raises(ValueError):
        keyspace.obs_key('team', '', *args)
    with pytest.raises(ValueError):
        keyspace.obs_key('user', 'a/b', *args)  # slug must not contain separators
    with pytest.raises(ValueError):
        keyspace.obs_key('user', 'a*', *args)  # nor wildcards
    with pytest.raises(ValueError):
        keyspace.obs_key('org', '', *args)  # unknown tier


def test_mirror_key_helpers_are_marker_relative() -> None:
    obs = f'mem/user/hwata/obs/claude/cc/pc1/s1/{_ID}'
    tomb = f'mem/user/hwata/tomb/claude/cc/pc1/s1/{_ID}'
    assert keyspace.mirror_to_tomb_key(obs) == tomb
    assert keyspace.mirror_to_obs_key(tomb) == obs
    legacy_obs = f'mem/obs/claude/cc/pc1/s1/{_ID}'
    assert keyspace.mirror_to_obs_key(keyspace.mirror_to_tomb_key(legacy_obs)) == legacy_obs


def test_broadcast_selectors_cover_all_namespaces() -> None:
    obs_sel = zenoh.KeyExpr(keyspace.broadcast_obs_selector(_ID))
    tomb_sel = zenoh.KeyExpr(keyspace.broadcast_tomb_selector(_ID))
    assert obs_sel.intersects(zenoh.KeyExpr(f'mem/obs/f/c/p/s/{_ID}'))
    assert obs_sel.intersects(zenoh.KeyExpr(f'mem/user/hwata/obs/f/c/p/s/{_ID}'))
    assert tomb_sel.intersects(zenoh.KeyExpr(f'mem/team/x/tomb/f/c/p/s/{_ID}'))
    assert not obs_sel.intersects(zenoh.KeyExpr(f'mem/obs/f/c/p/s/{"b" * 32}'))


# ---------------------------------------------------------------------------
# ADR-0019 Phase D: is_legacy_key and _is_legacy_read_fallback_on unit tests
# ---------------------------------------------------------------------------


def test_is_legacy_key_identifies_legacy_obs() -> None:
    assert keyspace.is_legacy_key(f'mem/obs/f/c/p/s/{_ID}')
    assert keyspace.is_legacy_key(f'mem/tomb/f/c/p/s/{_ID}')


def test_is_legacy_key_rejects_tiered_namespaces() -> None:
    assert not keyspace.is_legacy_key(f'mem/mesh/obs/f/c/p/s/{_ID}')
    assert not keyspace.is_legacy_key(f'mem/user/hwata/obs/f/c/p/s/{_ID}')
    assert not keyspace.is_legacy_key(f'mem/team/kioku/obs/f/c/p/s/{_ID}')


def test_is_legacy_key_rejects_non_mem_prefix() -> None:
    assert not keyspace.is_legacy_key(f'other/obs/f/c/p/s/{_ID}')
    assert not keyspace.is_legacy_key('obs/f/c/p/s')
    assert not keyspace.is_legacy_key('')


def test_is_legacy_read_fallback_on_returns_false_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('KIOKU_MESH_LEGACY_READ_FALLBACK', raising=False)
    assert cfg_mod._is_legacy_read_fallback_on() is False


def test_is_legacy_read_fallback_on_returns_true_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('KIOKU_MESH_LEGACY_READ_FALLBACK', 'on')
    assert cfg_mod._is_legacy_read_fallback_on() is True


def test_is_legacy_read_fallback_on_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('KIOKU_MESH_LEGACY_READ_FALLBACK', 'ON')
    assert cfg_mod._is_legacy_read_fallback_on() is True


# ---------------------------------------------------------------------------
# Thread-safety tests (R1 cross-review finding)
# ---------------------------------------------------------------------------


class _SlowLogger:
    """Logger stub whose warning() sleeps briefly to expose lock races."""

    def __init__(self, sleep_sec: float = 0.02) -> None:
        self._sleep_sec = sleep_sec
        self.calls: list[tuple] = []

    def warning(self, *args: object, **kwargs: object) -> None:
        time.sleep(self._sleep_sec)
        self.calls.append(args)


def test_warn_legacy_read_hit_once_is_thread_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """_warn_legacy_read_hit_once must emit exactly one WARNING under 20 concurrent calls."""
    slow_log = _SlowLogger()
    monkeypatch.setattr(cfg_mod, '_log', slow_log)
    monkeypatch.setattr(cfg_mod, '_legacy_read_hit_warned', False)

    threads = [threading.Thread(target=cfg_mod._warn_legacy_read_hit_once) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(slow_log.calls) == 1, f'expected exactly 1 WARNING, got {len(slow_log.calls)}'


def test_is_legacy_read_fallback_on_warns_once_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_is_legacy_read_fallback_on must emit exactly one WARNING under 20 concurrent calls."""
    monkeypatch.setenv('KIOKU_MESH_LEGACY_READ_FALLBACK', 'on')
    slow_log = _SlowLogger()
    monkeypatch.setattr(cfg_mod, '_log', slow_log)
    monkeypatch.setattr(cfg_mod, '_legacy_read_fallback_warned', False)

    threads = [threading.Thread(target=cfg_mod._is_legacy_read_fallback_on) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(slow_log.calls) == 1, f'expected exactly 1 WARNING, got {len(slow_log.calls)}'


# ---------------------------------------------------------------------------
# ADR-0029 PR 1: strengthened deprecation warning content
# ---------------------------------------------------------------------------


def test_legacy_read_fallback_warning_mentions_v1_and_migration_commands(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warning must cover v1.0 removal and the doctor/migrate-visibility commands (ADR-0029)."""
    monkeypatch.setenv('KIOKU_MESH_LEGACY_READ_FALLBACK', 'on')
    monkeypatch.setattr(cfg_mod, '_legacy_read_fallback_warned', False)

    with caplog.at_level('WARNING'):
        cfg_mod._is_legacy_read_fallback_on()

    messages = [r.message for r in caplog.records]
    assert len(messages) == 1, f'expected exactly 1 WARNING record, got {len(messages)}'
    msg = messages[0]
    assert 'v1.0' in msg
    assert 'doctor --check-legacy-namespace' in msg
    assert 'migrate-visibility' in msg


def test_legacy_read_fallback_warning_still_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Calling twice must still emit exactly one WARNING record (no per-record regression)."""
    monkeypatch.setenv('KIOKU_MESH_LEGACY_READ_FALLBACK', 'on')
    monkeypatch.setattr(cfg_mod, '_legacy_read_fallback_warned', False)

    with caplog.at_level('WARNING'):
        cfg_mod._is_legacy_read_fallback_on()
        cfg_mod._is_legacy_read_fallback_on()

    assert len(caplog.records) == 1
