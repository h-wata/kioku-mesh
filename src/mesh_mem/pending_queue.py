"""Alias to mesh_mem.memory.pending_queue (ADR-0023 — core/memory 層分離).

pending_queue は src/mesh_mem/memory/pending_queue.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import mesh_mem.memory.pending_queue as _real

_sys.modules[__name__] = _real
