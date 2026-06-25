"""Backward-compatibility shim: ``mesh_mem`` → ``kioku_mesh`` (ADR-0024).

.. deprecated::
    The ``mesh_mem`` package has been renamed to ``kioku_mesh``.  This shim
    re-exports everything from ``kioku_mesh`` so existing ``import mesh_mem``
    code continues to work, but emits :class:`DeprecationWarning` on import.

    **Removal target**: ``v1.0.0`` (see ADR-0024 for details).

    Migration::

        # Before
        from mesh_mem import MeshMem           # → DeprecationWarning

        # After
        from kioku_mesh import MeshMem         # OK
"""

import warnings

warnings.warn(
    "The 'mesh_mem' package has been renamed to 'kioku_mesh'. "
    "Please update your imports: replace 'mesh_mem' with 'kioku_mesh'. "
    "'mesh_mem' will be removed in v1.0.0 (see ADR-0024).",
    DeprecationWarning,
    stacklevel=2,
)

from kioku_mesh import *  # noqa: E402, F401, F403
from kioku_mesh import __version__  # noqa: E402, F401
