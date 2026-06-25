"""Tests for kioku-mesh on-disk path resolution with legacy fallback (#128)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kioku_mesh import paths
from kioku_mesh.paths import resolve_app_dir


@pytest.fixture(autouse=True)
def _reset_warn_cache() -> None:
    paths._warned.clear()


def test_fresh_returns_new_dir(tmp_path: Path) -> None:
    # Neither kioku-mesh nor mesh-mem exists -> prefer the new name.
    assert resolve_app_dir(tmp_path) == tmp_path / 'kioku-mesh'


def test_prefers_new_when_new_exists(tmp_path: Path) -> None:
    (tmp_path / 'kioku-mesh').mkdir()
    assert resolve_app_dir(tmp_path) == tmp_path / 'kioku-mesh'


def test_falls_back_to_legacy_when_only_legacy_exists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'mesh-mem').mkdir()
    assert resolve_app_dir(tmp_path) == tmp_path / 'mesh-mem'
    err = capsys.readouterr().err
    assert 'legacy path' in err
    assert 'mv ' in err  # nudges a manual move, does not perform it


def test_legacy_fallback_does_not_move_anything(tmp_path: Path) -> None:
    legacy = tmp_path / 'mesh-mem'
    legacy.mkdir()
    (legacy / 'marker').write_text('x', encoding='utf-8')
    resolve_app_dir(tmp_path)
    # Nothing moved: legacy intact, new not created.
    assert (legacy / 'marker').exists()
    assert not (tmp_path / 'kioku-mesh').exists()


def test_both_exist_prefers_new_and_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'kioku-mesh').mkdir()
    (tmp_path / 'mesh-mem').mkdir()
    assert resolve_app_dir(tmp_path) == tmp_path / 'kioku-mesh'
    assert 'both' in capsys.readouterr().err


def test_warning_emitted_once_per_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / 'mesh-mem').mkdir()
    resolve_app_dir(tmp_path)
    resolve_app_dir(tmp_path)
    # Two calls, one warning line.
    assert capsys.readouterr().err.count('legacy path') == 1
