"""Tests for ADR-0024: mesh_mem → kioku_mesh package rename.

Covers:
- Basic import from kioku_mesh works without warnings
- import mesh_mem emits DeprecationWarning (shim)
- MESH_MEM_* env vars fall back with DeprecationWarning
"""

import importlib
import sys
import warnings

import pytest


class TestKiokuMeshImport:
    def test_import_top_level(self) -> None:
        import kioku_mesh  # noqa: F401

        assert kioku_mesh.__version__

    def test_import_observation(self) -> None:
        from kioku_mesh.core.models import Observation  # noqa: F401

        assert Observation is not None

    def test_import_no_deprecation_warning(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter('error', DeprecationWarning)
            importlib.import_module('kioku_mesh')


class TestMeshMemShim:
    def test_import_emits_deprecation_warning(self) -> None:
        sys.modules.pop('mesh_mem', None)
        with pytest.warns(DeprecationWarning, match="renamed to 'kioku_mesh'"):
            import mesh_mem  # noqa: F401

    def test_shim_exports_version(self) -> None:
        sys.modules.pop('mesh_mem', None)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            import mesh_mem

        from kioku_mesh import __version__ as km_ver

        assert mesh_mem.__version__ == km_ver

    def test_shim_exports_version_attr(self) -> None:
        sys.modules.pop('mesh_mem', None)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            import mesh_mem

        assert hasattr(mesh_mem, '__version__')


class TestEnvVarCompat:
    def test_new_key_used_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kioku_mesh.core._env_compat import get_env

        monkeypatch.setenv('KIOKU_MESH_STATE_DIR', '/tmp/new')
        monkeypatch.delenv('MESH_MEM_STATE_DIR', raising=False)
        with warnings.catch_warnings():
            warnings.simplefilter('error', DeprecationWarning)
            assert get_env('KIOKU_MESH_STATE_DIR') == '/tmp/new'

    def test_legacy_key_fallback_with_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kioku_mesh.core._env_compat import get_env

        monkeypatch.delenv('KIOKU_MESH_STATE_DIR', raising=False)
        monkeypatch.setenv('MESH_MEM_STATE_DIR', '/tmp/legacy')
        with pytest.warns(DeprecationWarning, match='MESH_MEM_STATE_DIR'):
            val = get_env('KIOKU_MESH_STATE_DIR')
        assert val == '/tmp/legacy'

    def test_new_key_takes_precedence_over_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kioku_mesh.core._env_compat import get_env

        monkeypatch.setenv('KIOKU_MESH_STATE_DIR', '/tmp/new')
        monkeypatch.setenv('MESH_MEM_STATE_DIR', '/tmp/legacy')
        with warnings.catch_warnings():
            warnings.simplefilter('error', DeprecationWarning)
            val = get_env('KIOKU_MESH_STATE_DIR')
        assert val == '/tmp/new'

    def test_default_returned_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kioku_mesh.core._env_compat import get_env

        monkeypatch.delenv('KIOKU_MESH_STATE_DIR', raising=False)
        monkeypatch.delenv('MESH_MEM_STATE_DIR', raising=False)
        assert get_env('KIOKU_MESH_STATE_DIR', '/default') == '/default'

    def test_invalid_prefix_raises(self) -> None:
        from kioku_mesh.core._env_compat import get_env

        with pytest.raises(ValueError, match='KIOKU_MESH_'):
            get_env('MESH_MEM_STATE_DIR')
