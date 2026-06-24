"""Alias to mesh_mem.memory.purge (ADR-0023 — core/memory 層分離).

purge は src/mesh_mem/memory/purge.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import mesh_mem.memory.purge as _real

_sys.modules[__name__] = _real
