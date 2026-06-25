"""Alias to kioku_mesh.memory.replication (ADR-0023 — core/memory 層分離).

replication は src/kioku_mesh/memory/replication.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import kioku_mesh.memory.replication as _real

_sys.modules[__name__] = _real
