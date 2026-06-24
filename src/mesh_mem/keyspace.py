"""Alias to mesh_mem.core.keyspace (ADR-0023 — core/memory 層分離).

keyspace は src/mesh_mem/core/keyspace.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import mesh_mem.core.keyspace as _real

_sys.modules[__name__] = _real
