"""Backward-compatibility shim: ``mesh_mem`` → ``kioku_mesh`` (ADR-0024).

.. deprecated::
    The ``mesh_mem`` package has been renamed to ``kioku_mesh``.  This shim
    re-exports everything from ``kioku_mesh`` so existing ``import mesh_mem``
    code continues to work, but emits :class:`DeprecationWarning` on import.

    **Removal target**: ``v1.0.0`` (see ADR-0024 for details).

    Migration::

        # Before
        from mesh_mem import MeshMem           # → DeprecationWarning
        from mesh_mem.local_index import ...   # → DeprecationWarning

        # After
        from kioku_mesh import MeshMem         # OK
        from kioku_mesh.memory.local_index import ...  # OK
"""

import importlib
import importlib.abc
import importlib.machinery
import sys
import warnings

_REMOVAL = 'v1.0.0'

warnings.warn(
    "The 'mesh_mem' package has been renamed to 'kioku_mesh'. "
    "Please update your imports: replace 'mesh_mem' with 'kioku_mesh'. "
    "'mesh_mem' will be removed in v1.0.0 (see ADR-0024).",
    DeprecationWarning,
    stacklevel=2,
)

from kioku_mesh import *  # noqa: E402, F401, F403
from kioku_mesh import __version__  # noqa: E402, F401

# --- submodule redirect (B1) ---
# Intercept `from mesh_mem.X import Y` / `import mesh_mem.X` so they
# redirect to kioku_mesh.X with a DeprecationWarning.


class _SubmoduleShimLoader(importlib.abc.Loader):
    """Load mesh_mem.X as a thin alias for kioku_mesh.X."""

    def __init__(self, old: str, new: str) -> None:
        self._old = old
        self._new = new

    def create_module(self, spec) -> None:  # noqa: ANN001
        return None

    def exec_module(self, module) -> None:  # noqa: ANN001
        warnings.warn(
            f"'{self._old}' is deprecated; use '{self._new}' instead. "
            f"'mesh_mem.*' will be removed in {_REMOVAL} (ADR-0024).",
            DeprecationWarning,
            stacklevel=1,
        )
        try:
            real = importlib.import_module(self._new)
        except ImportError:
            return
        vars(module).update(
            {
                k: v
                for k, v in vars(real).items()
                if not k.startswith('__') or k in ('__all__', '__version__', '__doc__')
            }
        )
        if hasattr(real, '__path__'):
            module.__path__ = real.__path__
            module.__package__ = real.__package__
        sys.modules[self._old] = real


class _MeshMemShimFinder(importlib.abc.MetaPathFinder):
    """Redirect any mesh_mem.* import to kioku_mesh.*."""

    def find_spec(self, fullname, path, target=None):  # noqa: ANN001, ANN202
        if not fullname.startswith('mesh_mem.'):
            return None
        new_name = 'kioku_mesh.' + fullname[len('mesh_mem.') :]
        loader = _SubmoduleShimLoader(fullname, new_name)
        return importlib.machinery.ModuleSpec(fullname, loader)


if not any(isinstance(f, _MeshMemShimFinder) for f in sys.meta_path):
    sys.meta_path.append(_MeshMemShimFinder())
