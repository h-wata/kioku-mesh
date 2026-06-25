"""Alias to kioku_mesh.memory.local_index (ADR-0023 — core/memory 層分離).

local_index は src/kioku_mesh/memory/local_index.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import kioku_mesh.memory.local_index as _real

_sys.modules[__name__] = _real
