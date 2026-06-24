"""Alias to mesh_mem.core.models (ADR-0023 — core/memory 層分離).

models は src/mesh_mem/core/models.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import mesh_mem.core.models as _real

_sys.modules[__name__] = _real
