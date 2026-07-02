"""Tests for ADR-0024 / ADR-0029: mesh_mem → kioku_mesh package rename, v1.0 removal.

Covers:
- Basic import from kioku_mesh works without warnings
- import mesh_mem raises ImportError (compat shim removed in v1.0, ADR-0029 PR 4)
- MESH_MEM_* env vars are ignored, no fallback (ADR-0029 PR 4)
"""

import importlib
import sys

import pytest


class TestKiokuMeshImport:
    def test_import_top_level(self) -> None:
        import kioku_mesh  # noqa: F401

        assert kioku_mesh.__version__

    def test_import_observation(self) -> None:
        from kioku_mesh.core.models import Observation  # noqa: F401

        assert Observation is not None


class TestMeshMemShimRemoved:
    """ADR-0029 PR 4: the mesh_mem import compat shim is removed in v1.0."""

    def test_import_raises_import_error(self) -> None:
        sys.modules.pop('mesh_mem', None)
        with pytest.raises(ImportError):
            import mesh_mem  # noqa: F401

    def test_submodule_import_raises_import_error(self) -> None:
        for key in list(sys.modules):
            if key == 'mesh_mem' or key.startswith('mesh_mem.'):
                sys.modules.pop(key, None)
        with pytest.raises(ImportError):
            importlib.import_module('mesh_mem.local_index')

    def test_env_compat_module_removed(self) -> None:
        with pytest.raises(ImportError):
            importlib.import_module('kioku_mesh.core._env_compat')


class TestLegacyEnvVarIgnored:
    """ADR-0029 PR 4: MESH_MEM_* fallback is removed; only KIOKU_MESH_* is read."""

    def test_legacy_state_dir_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kioku_mesh.core import identity

        monkeypatch.delenv('KIOKU_MESH_STATE_DIR', raising=False)
        monkeypatch.setenv('MESH_MEM_STATE_DIR', '/tmp/legacy-should-be-ignored')
        importlib.reload(identity)
        try:
            resolved = identity.state_dir()
            assert str(resolved) != '/tmp/legacy-should-be-ignored'
        finally:
            monkeypatch.delenv('MESH_MEM_STATE_DIR', raising=False)
            importlib.reload(identity)

    def test_legacy_user_id_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kioku_mesh.core import config

        monkeypatch.delenv('KIOKU_MESH_USER_ID', raising=False)
        monkeypatch.setenv('MESH_MEM_USER_ID', 'legacy-user')
        assert config.get_user_id() != 'legacy-user'
