"""Alias to kioku_mesh.core.identity (ADR-0023 — core/memory 層分離).

identity は src/kioku_mesh/core/identity.py に移動しました。
このファイルは後方互換のためモジュールエイリアスとして残しています。
"""

import sys as _sys

import kioku_mesh.core.identity as _real

_sys.modules[__name__] = _real
