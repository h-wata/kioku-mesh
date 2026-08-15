"""Unit tests for the read-side project-alias table (TASK-361)."""

from kioku_mesh.core.project_alias import expand_project_aliases
from kioku_mesh.core.project_alias import normalize_project_filter
from kioku_mesh.core.project_alias import resolve_project_alias


def test_resolve_known_aliases_to_canonical() -> None:
    assert resolve_project_alias('mesh-mem') == 'kioku-mesh'
    assert resolve_project_alias('/home/gisen/work/mesh-mem') == 'kioku-mesh'
    assert resolve_project_alias('portable_colorized_scanner') == 'portable-scanner'
    assert resolve_project_alias('rmf') == 'rmf_ws'


def test_resolve_unknown_and_empty_pass_through() -> None:
    assert resolve_project_alias('kioku-mesh') == 'kioku-mesh'
    assert resolve_project_alias('') == ''


def test_expand_includes_all_legacy_names_for_canonical() -> None:
    # kioku-mesh has two legacy spellings now: mesh-mem and the path artifact.
    expanded = expand_project_aliases('kioku-mesh')
    assert expanded[0] == 'kioku-mesh'
    assert set(expanded[1:]) == {'mesh-mem', '/home/gisen/work/mesh-mem'}


def test_expand_from_legacy_name_returns_canonical_group() -> None:
    assert set(expand_project_aliases('rmf')) == {'rmf_ws', 'rmf'}


def test_normalize_project_filter_dedupes_and_drops_empty() -> None:
    assert normalize_project_filter(expand_project_aliases('portable_colorized_scanner')) == (
        'portable-scanner',
        'portable_colorized_scanner',
    )
    assert normalize_project_filter('') == ()
