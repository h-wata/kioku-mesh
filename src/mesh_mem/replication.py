"""Alias to mesh_mem.memory.replication (ADR-0023 — core/memory 層分離).

replication は src/mesh_mem/memory/replication.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import mesh_mem.memory.replication as _real

_sys.modules[__name__] = _real
