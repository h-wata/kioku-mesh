"""Alias to kioku_mesh.memory.purge (ADR-0023 — core/memory 層分離).

purge は src/kioku_mesh/memory/purge.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import kioku_mesh.memory.purge as _real

_sys.modules[__name__] = _real
