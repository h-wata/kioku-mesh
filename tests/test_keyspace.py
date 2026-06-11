"""Unit tests for the namespace-aware key vocabulary (ADR-0019 Phase A)."""

from __future__ import annotations

import zenoh

from mesh_mem import keyspace

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
