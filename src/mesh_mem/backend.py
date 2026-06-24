"""Alias to mesh_mem.memory.backend (ADR-0023 — core/memory 層分離).

backend は src/mesh_mem/memory/backend.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import mesh_mem.memory.backend as _real

_sys.modules[__name__] = _real
