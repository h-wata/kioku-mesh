"""Alias to mesh_mem.core.paths (ADR-0023 — core/memory 層分離).

paths は src/mesh_mem/core/paths.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import mesh_mem.core.paths as _real

_sys.modules[__name__] = _real
