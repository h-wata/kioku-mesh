"""ADR-0023 layering rule verification via static AST analysis.

Enforces that memory/messaging/bridge layers do not cross-depend in ways
prohibited by ADR-0023, without importing modules or triggering side effects.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / 'src' / 'mesh_mem'

CORE_PKG = 'mesh_mem.core'
MEMORY_PKG = 'mesh_mem.memory'
MESSAGING_PKG = 'mesh_mem.messaging'
BRIDGE_PKG = 'mesh_mem.bridge'


def _collect_imports(pkg_dir: Path) -> dict[str, list[str]]:
    """Return {filename: [imported_module, ...]} for all .py files in pkg_dir."""
    result: dict[str, list[str]] = {}
    for py_file in sorted(pkg_dir.glob('*.py')):
        if py_file.name == '__init__.py':
            continue
        tree = ast.parse(py_file.read_text(encoding='utf-8'))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        result[py_file.name] = imports
    return result


def _build_absolute_module(relative_level: int, base_pkg: str, module: str | None) -> str:
    """Resolve a relative import to its absolute module name."""
    parts = base_pkg.split('.')
    parent_parts = parts[: len(parts) - (relative_level - 1)]
    base = '.'.join(parent_parts)
    if module:
        return f'{base}.{module}'
    return base


def _collect_absolute_imports(pkg_dir: Path, pkg_name: str) -> dict[str, list[str]]:
    """Return {filename: [absolute_module, ...]} resolving relative imports."""
    result: dict[str, list[str]] = {}
    for py_file in sorted(pkg_dir.glob('*.py')):
        if py_file.name == '__init__.py':
            continue
        tree = ast.parse(py_file.read_text(encoding='utf-8'))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    abs_mod = _build_absolute_module(node.level, pkg_name, node.module)
                    imports.append(abs_mod)
                elif node.module:
                    imports.append(node.module)
        result[py_file.name] = imports
    return result


def test_core_does_not_import_memory() -> None:
    """ADR-0023: core layer must not import memory layer."""
    core_imports = _collect_absolute_imports(SRC_ROOT / 'core', CORE_PKG)
    violations: list[str] = []
    for filename, imports in core_imports.items():
        for imp in imports:
            if imp.startswith(MEMORY_PKG):
                violations.append(f'{filename}: imports {imp!r}')
    assert not violations, 'core 層が memory 層に依存しています (ADR-0023 違反):\n' + '\n'.join(violations)


def test_core_does_not_import_messaging() -> None:
    """ADR-0023: core layer must not import messaging layer."""
    core_imports = _collect_absolute_imports(SRC_ROOT / 'core', CORE_PKG)
    violations: list[str] = []
    for filename, imports in core_imports.items():
        for imp in imports:
            if imp.startswith(MESSAGING_PKG):
                violations.append(f'{filename}: imports {imp!r}')
    assert not violations, 'core 層が messaging 層に依存しています (ADR-0023 違反):\n' + '\n'.join(violations)


def test_memory_does_not_import_messaging() -> None:
    """ADR-0023: memory layer must not directly import messaging layer."""
    memory_imports = _collect_absolute_imports(SRC_ROOT / 'memory', MEMORY_PKG)
    violations: list[str] = []
    for filename, imports in memory_imports.items():
        for imp in imports:
            if imp.startswith(MESSAGING_PKG):
                violations.append(f'{filename}: imports {imp!r}')
    assert not violations, 'memory 層が messaging 層に直接依存しています (ADR-0023 違反):\n' + '\n'.join(violations)


def test_memory_does_not_import_bridge() -> None:
    """ADR-0023: memory layer must not directly import bridge layer."""
    memory_imports = _collect_absolute_imports(SRC_ROOT / 'memory', MEMORY_PKG)
    violations: list[str] = []
    for filename, imports in memory_imports.items():
        for imp in imports:
            if imp.startswith(BRIDGE_PKG):
                violations.append(f'{filename}: imports {imp!r}')
    assert not violations, 'memory 層が bridge 層に直接依存しています (ADR-0023 違反):\n' + '\n'.join(violations)


def test_core_files_exist() -> None:
    """All expected core/ modules are present."""
    expected = {'transport.py', 'tls.py', 'identity.py', 'keyspace.py', 'config.py', 'paths.py', 'models.py'}
    actual = {f.name for f in (SRC_ROOT / 'core').glob('*.py') if f.name != '__init__.py'}
    missing = expected - actual
    assert not missing, f'core/ に不足しているファイル: {missing}'


def test_memory_files_exist() -> None:
    """All expected memory/ modules are present."""
    expected = {'store.py', 'local_index.py', 'pending_queue.py', 'purge.py', 'backend.py', 'replication.py'}
    actual = {f.name for f in (SRC_ROOT / 'memory').glob('*.py') if f.name != '__init__.py'}
    missing = expected - actual
    assert not missing, f'memory/ に不足しているファイル: {missing}'


def test_stub_layers_exist() -> None:
    """Stub packages for messaging/ and bridge/ layers exist."""
    assert (SRC_ROOT / 'messaging' / '__init__.py').exists(), 'messaging/__init__.py が存在しません'
    assert (SRC_ROOT / 'bridge' / '__init__.py').exists(), 'bridge/__init__.py が存在しません'
