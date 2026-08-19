"""Golden tests pinning the CLI parser tree, as a refactor safety net.

``_build_parser`` is split into per-subcommand-family registration functions
(TASK-482). Two things break silently under that refactor and nothing else in
the suite catches either:

* **Registration order is the ``--help`` display order.** Calling the
  registration functions in a different order than the original inline blocks
  changes what every user sees, while every command keeps working.
* **A dropped ``set_defaults(func=...)`` is invisible until runtime.** Import
  succeeds, ``--help`` is unchanged, and the subcommand only breaks when
  somebody actually runs it and ``args.func`` is missing.

So the goldens pin both the rendered help of every parser node *and* the
structural attributes (dest / default / choices / metavar / nargs / type /
``func`` binding) that help output does not show. They were generated from the
parser as it stood before the split and must survive it unchanged.

Regenerate deliberately, and review the diff, when a CLI change is intended::

    KIOKU_MESH_REGEN_CLI_GOLDEN=1 uv run pytest tests/test_cli_parser_golden.py

Comparison is per-line ``rstrip``-ed so the trailing-whitespace pre-commit hook
cannot desynchronise the goldens from the parser they describe.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
import contextlib
import os
from pathlib import Path

from kioku_mesh.__main__ import _build_parser

_GOLDEN_DIR = Path(__file__).parent / 'data'
_HELP_GOLDEN = _GOLDEN_DIR / 'cli_help.txt'
_STRUCTURE_GOLDEN = _GOLDEN_DIR / 'cli_parser_structure.txt'

# argparse derives its wrap width from the terminal, so pin it or the golden
# only matches on whatever terminal happened to generate it.
_HELP_COLUMNS = '100'

_REGEN_ENV = 'KIOKU_MESH_REGEN_CLI_GOLDEN'


@contextlib.contextmanager
def _fixed_terminal_width() -> Iterator[None]:
    """Pin ``COLUMNS`` so ``format_help`` wraps identically everywhere."""
    previous = os.environ.get('COLUMNS')
    os.environ['COLUMNS'] = _HELP_COLUMNS
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('COLUMNS', None)
        else:
            os.environ['COLUMNS'] = previous


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _iter_nodes(
    parser: argparse.ArgumentParser,
    path: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Yield every parser in the tree, in registration order, depth first.

    ``_SubParsersAction.choices`` is the insertion-ordered name -> parser map,
    so walking it preserves exactly the order ``--help`` lists.
    """
    yield path, parser
    action = _subparsers_action(parser)
    if action is not None:
        for name, subparser in action.choices.items():
            yield from _iter_nodes(subparser, (*path, name))


def _stable_repr(value: object) -> str:
    """Repr ``value`` without the memory address that plain repr leaks."""
    text = repr(value)
    if ' object at 0x' in text:
        return f'<{type(value).__name__}>'
    return text


def _describe_action(action: argparse.Action) -> str:
    if isinstance(action, argparse._SubParsersAction):
        choices = f'[{", ".join(action.choices)}]'
    elif action.choices is None:
        choices = 'None'
    else:
        choices = _stable_repr(list(action.choices))
    type_name = 'None' if action.type is None else getattr(action.type, '__name__', _stable_repr(action.type))
    return ' '.join(
        [
            f'action={type(action).__name__}',
            f'opts={list(action.option_strings)}',
            f'dest={action.dest}',
            f'nargs={action.nargs!r}',
            f'const={_stable_repr(action.const)}',
            f'default={_stable_repr(action.default)}',
            f'choices={choices}',
            f'required={action.required}',
            f'metavar={action.metavar!r}',
            f'type={type_name}',
            f'help={action.help!r}',
            f'completer={hasattr(action, "completer")}',
        ]
    )


def _node_title(path: tuple[str, ...]) -> str:
    return ' '.join(('kioku-mesh', *path))


def _render_help_dump(parser: argparse.ArgumentParser) -> str:
    lines: list[str] = []
    with _fixed_terminal_width():
        for path, node in _iter_nodes(parser):
            lines.append(f'### {_node_title(path)}')
            lines.append(node.format_help())
    return '\n'.join(lines) + '\n'


def _render_structure_dump(parser: argparse.ArgumentParser) -> str:
    lines: list[str] = []
    for path, node in _iter_nodes(parser):
        lines.append(f'### {_node_title(path)}')
        func = node.get_default('func')
        lines.append(f'func={getattr(func, "__name__", _stable_repr(func))}')
        lines.append(f'description={node.description!r}')
        for action in node._actions:
            lines.append(f'  {_describe_action(action)}')
    return '\n'.join(lines) + '\n'


def _normalized(text: str) -> list[str]:
    """Split into lines, ignoring trailing whitespace and trailing blank lines.

    The trailing-whitespace and end-of-file pre-commit hooks rewrite the golden
    files on commit; without this the goldens would desync from the parser they
    describe the first time someone commits them.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _check_against_golden(path: Path, actual: str, what: str) -> None:
    if os.environ.get(_REGEN_ENV) == '1':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding='utf-8')
        return
    assert path.exists(), f'{what} golden missing: {path} (regenerate with {_REGEN_ENV}=1)'
    expected = path.read_text(encoding='utf-8')
    assert _normalized(actual) == _normalized(expected), (
        f'{what} changed. If the CLI change is intended, regenerate with {_REGEN_ENV}=1 and review the diff.'
    )


def test_help_output_matches_golden() -> None:
    """Pin ``--help`` for the top-level parser and every subcommand."""
    _check_against_golden(_HELP_GOLDEN, _render_help_dump(_build_parser()), 'CLI help output')


def test_parser_structure_matches_golden() -> None:
    """Pin dest / default / choices / metavar / type and the ``func`` binding.

    Help text does not show ``dest`` or most defaults, so a refactor could keep
    ``--help`` byte-identical while rebinding an argument. This golden is what
    makes "no behaviour change" checkable.
    """
    _check_against_golden(_STRUCTURE_GOLDEN, _render_structure_dump(_build_parser()), 'CLI parser structure')


def test_every_subcommand_binds_a_func() -> None:
    """Every runnable subcommand must reach a handler.

    Unlike the goldens this needs no baseline, so it also guards subcommands
    added after this refactor: a new leaf without ``set_defaults(func=...)``
    fails here instead of at the user's shell.
    """
    missing: list[str] = []
    for path, node in _iter_nodes(_build_parser()):
        if not path or _subparsers_action(node) is not None:
            # The root and group nodes (``mcp``, ``tls``, ...) dispatch to
            # children; argparse rejects them on their own via required=True.
            continue
        if not callable(node.get_default('func')):
            missing.append(_node_title(path))
    assert missing == [], f'subcommands with no func binding: {missing}'
