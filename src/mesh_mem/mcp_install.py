"""Register ``mesh-mem-mcp`` with supported MCP clients (#85).

v0.3 scope: Claude Code (via ``claude mcp add``) and Codex CLI (via direct
edit of ``~/.codex/config.toml``). Claude Desktop is deferred until #87
(macOS / Windows verification); Gemini CLI is deferred to v0.4. Both gaps
are intentional — the manual recipes in ``docs/mcp-clients.md`` still
cover those clients.

Design notes:
- The absolute path to ``mesh-mem-mcp`` is resolved at install time
  (``shutil.which``) and baked into the registration. The MCP launcher
  (Claude Code, Codex CLI) is invoked from environments that may not
  inherit an interactive shell's PATH, so PATH-relative invocations break.
- Each client's installer is a pure function that takes its probe
  dependencies (subprocess runner, config path) as arguments so tests
  can drive without monkeypatching globals.
- Codex CLI's TOML config is edited via line-based block substitution
  rather than a TOML round-trip — re-serializing the whole file would
  drop user comments and reformat unrelated sections. Block-level
  substitution preserves everything outside ``[mcp_servers.<name>]``
  and its nested tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tomllib
from typing import Callable


class MCPClient(str, Enum):
    """MCP clients that ``mesh-mem mcp install`` supports."""

    CLAUDE_CODE = 'claude-code'
    CODEX_CLI = 'codex-cli'


# Family / client_id defaults per client. Users can override via --env if needed.
_DEFAULT_FAMILY: dict[MCPClient, str] = {
    MCPClient.CLAUDE_CODE: 'claude',
    MCPClient.CODEX_CLI: 'codex',
}
_DEFAULT_CLIENT_ID: dict[MCPClient, str] = {
    MCPClient.CLAUDE_CODE: 'claude-code',
    MCPClient.CODEX_CLI: 'codex-cli',
}

# Registration key default. Underscore form matches existing docs/mcp-clients.md
# examples and the most common existing installs in the wild; TOML accepts both
# without quoting so the choice is purely conventional.
DEFAULT_REGISTRY_NAME = 'mesh_mem'

# Default Zenoh transport endpoint baked into installed env. Matches the same
# default used by store.py:get_session().
_DEFAULT_ZENOH_CONNECT = 'tcp/127.0.0.1:7447'


@dataclass(frozen=True)
class InstallPlan:
    """All the info one client installer needs.

    ``command`` is the absolute path to ``mesh-mem-mcp``. ``env`` is the
    fully-resolved env block (defaults already merged with user overrides).
    """

    client: MCPClient
    name: str
    command: str
    env: dict[str, str] = field(default_factory=dict)


# TOML bare keys allow only ASCII letters, digits, underscore, and hyphen
# (per https://toml.io/en/v1.0.0#keys). A name with any other character —
# notably ``.`` — would either generate invalid TOML or, worse, silently
# rewrite the wrong table because ``[mcp_servers.foo.bar]`` is a nested
# table by spec. The same regex is the safest also-good-as-a-claude-MCP-name
# constraint (Claude Code's CLI doesn't formally publish a charset but ASCII
# alphanumerics + `_-` covers everything in docs/mcp-clients.md examples).
_VALID_REGISTRY_NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def _validate_registry_name(name: str) -> None:
    """Reject registry keys that wouldn't survive both TOML and Claude CLI safely.

    Surfaces the rejection as ``ValueError`` so the CLI layer maps it to a
    documented exit code rather than ``[mcp_servers.foo.bar]`` silently
    landing as a nested table in the user's Codex config (Codex review on
    #97).
    """
    if not name or not _VALID_REGISTRY_NAME_RE.fullmatch(name):
        raise ValueError(
            f'registry name {name!r} must match [A-Za-z0-9_-]+ '
            '(no dots, spaces, or other characters that break TOML bare keys).'
        )


def build_install_plan(
    client: MCPClient,
    name: str = DEFAULT_REGISTRY_NAME,
    *,
    extra_env: dict[str, str] | None = None,
    mesh_mem_mcp_path: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> InstallPlan:
    """Resolve defaults into a fully-specified plan.

    Args:
        client: which MCP client to register with.
        name: registry key (e.g. ``mesh_mem`` or ``mesh-mem``).
        extra_env: extra env vars merged on top of the default mesh-mem env.
        mesh_mem_mcp_path: pin a specific binary path (for tests or non-PATH
            installs). When omitted, resolved via ``shutil.which``.
        which: PATH resolver, defaults to ``shutil.which``. Tests inject a fake.

    Raises:
        FileNotFoundError: when ``mesh-mem-mcp`` can't be resolved.
        ValueError: when ``name`` is not a TOML / Claude-safe bare key.
    """
    _validate_registry_name(name)
    resolver = which or shutil.which
    command = mesh_mem_mcp_path or resolver('mesh-mem-mcp')
    if not command:
        raise FileNotFoundError(
            'mesh-mem-mcp not on PATH. Install mesh-mem first '
            '(`uv tool install git+https://github.com/h-wata/mesh-mem.git`).'
        )
    env: dict[str, str] = {
        'ZENOH_CONNECT': _DEFAULT_ZENOH_CONNECT,
        'MESH_MEM_AGENT_FAMILY': _DEFAULT_FAMILY[client],
        'MESH_MEM_CLIENT_ID': _DEFAULT_CLIENT_ID[client],
    }
    if extra_env:
        env.update(extra_env)
    return InstallPlan(client=client, name=name, command=command, env=env)


# -- Claude Code (via `claude mcp add`) ----------------------------------------


def _build_claude_add_command(claude_binary: str, plan: InstallPlan) -> list[str]:
    """Build the ``claude mcp add`` argv. Pure — for both dry-run and execution."""
    cmd: list[str] = [claude_binary, 'mcp', 'add', plan.name, '-s', 'user']
    for key, value in plan.env.items():
        cmd.extend(['-e', f'{key}={value}'])
    cmd.append('--')
    cmd.append(plan.command)
    return cmd


def install_claude_code(
    plan: InstallPlan,
    *,
    force: bool = False,
    dry_run: bool = False,
    run: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str:
    """Register ``mesh-mem-mcp`` with Claude Code.

    The CLI route via ``claude mcp add`` is the only registration path that
    Claude Code actually reads — entries under ``~/.claude/settings.json``
    are silently ignored. See ``docs/mcp-clients.md`` §Claude Code.

    Returns the status message printed to the user.
    """
    resolver = which or shutil.which
    claude = resolver('claude')
    if not claude:
        raise FileNotFoundError(
            'claude binary not on PATH. Install Claude Code first (https://docs.claude.com/en/docs/claude-code).'
        )
    cmd = _build_claude_add_command(claude, plan)
    if dry_run:
        return ' '.join(shlex.quote(part) for part in cmd)

    runner = run or _default_subprocess_run

    # Best-effort dedupe: if ``plan.name`` is already listed we either refuse
    # or remove-then-add for an idempotent --force replace. The `claude mcp
    # list` output is line-oriented "<name>: <command>" so a substring match
    # against ``plan.name + ':'`` is good enough — exact tokenization isn't
    # needed.
    list_result = runner([claude, 'mcp', 'list'])
    if list_result.returncode == 0 and f'{plan.name}:' in list_result.stdout:
        if not force:
            return f'error: {plan.name!r} is already registered with Claude Code. Use --force to overwrite.'
        # The remove step has to succeed before we re-add; if Claude refuses
        # (permission, state mismatch) we want the underlying error rather
        # than a confusing "claude mcp add failed" downstream (Codex review #97).
        remove_result = runner([claude, 'mcp', 'remove', plan.name])
        if remove_result.returncode != 0:
            stderr = (remove_result.stderr or '').strip() or '(no stderr)'
            raise RuntimeError(f'claude mcp remove {plan.name} failed (rc={remove_result.returncode}): {stderr}')

    result = runner(cmd)
    if result.returncode != 0:
        stderr = (result.stderr or '').strip()
        raise RuntimeError(f'claude mcp add failed (rc={result.returncode}): {stderr}')
    return f'registered {plan.name} with Claude Code via {claude}'


def _default_subprocess_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` capturing stdout/stderr and never raising on non-zero rc."""
    return subprocess.run(argv, check=False, capture_output=True, text=True)


# -- Codex CLI (via ~/.codex/config.toml) --------------------------------------


def _default_codex_config_path() -> Path:
    """Return the Codex CLI config path. No env override exists upstream."""
    return Path.home() / '.codex' / 'config.toml'


def _render_codex_toml_block(plan: InstallPlan) -> str:
    """Render the TOML block for one ``[mcp_servers.<name>]`` entry.

    Inline-table form for env would be possible but the nested
    ``[mcp_servers.X.env]`` table matches the Codex CLI examples in the
    wild and is easier for users to edit by hand.
    """
    lines = [
        '# Added by `mesh-mem mcp install --client codex-cli`. Re-run with --force to update.',
        f'[mcp_servers.{plan.name}]',
        f'command = "{plan.command}"',
        '',
        f'[mcp_servers.{plan.name}.env]',
    ]
    for key, value in plan.env.items():
        lines.append(f'{key} = "{value}"')
    return '\n'.join(lines)


def _replace_codex_block(existing: str, name: str, new_block: str) -> str:
    """Replace the ``[mcp_servers.<name>]`` block (plus nested tables) in-place.

    Block extent: from the first table header line that starts
    ``[mcp_servers.<name>]`` OR ``[mcp_servers.<name>.`` (any nested
    sub-table) through the line before the next non-matching ``[``-prefixed
    table header (or EOF). Lines in between belong to our block by TOML
    semantics.

    Comments / blank lines immediately preceding the block are NOT swept —
    they survive into the result so user annotations on adjacent sections
    don't get clobbered.
    """
    lines = existing.split('\n')
    server_header = f'[mcp_servers.{name}]'
    nested_prefix = f'[mcp_servers.{name}.'

    start_idx: int | None = None
    end_idx = len(lines)
    for i, raw in enumerate(lines):
        stripped = raw.lstrip()
        if stripped.startswith(server_header) or stripped.startswith(nested_prefix):
            if start_idx is None:
                start_idx = i
            continue
        if start_idx is not None and stripped.startswith('['):
            end_idx = i
            break

    if start_idx is None:
        # No existing block — fall through to append at end of file.
        suffix = '' if existing.endswith('\n') else '\n'
        return existing + suffix + '\n' + new_block + '\n'

    new_lines = lines[:start_idx] + new_block.split('\n') + lines[end_idx:]
    return '\n'.join(new_lines)


def install_codex_cli(
    plan: InstallPlan,
    *,
    force: bool = False,
    dry_run: bool = False,
    config_path: Path | None = None,
) -> str:
    """Register ``mesh-mem-mcp`` with Codex CLI by editing ``config.toml``.

    Codex CLI reads ``mcp_servers.<name>`` tables from its TOML config.
    There is no upstream CLI command analogous to ``claude mcp add``, so
    direct config edit is the documented path.
    """
    target = config_path or _default_codex_config_path()
    block = _render_codex_toml_block(plan)
    if dry_run:
        return f'# would write to {target}\n{block}'

    if target.exists():
        existing_text = target.read_text(encoding='utf-8')
        try:
            data = tomllib.loads(existing_text)
        except tomllib.TOMLDecodeError as e:
            raise RuntimeError(f'cannot parse {target} as TOML: {e}') from e
        already = data.get('mcp_servers', {}).get(plan.name) is not None
        if already and not force:
            return f'error: mcp_servers.{plan.name} already exists in {target}. Use --force to overwrite.'
        new_text = _replace_codex_block(existing_text, plan.name, block)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        new_text = block + '\n'

    target.write_text(new_text, encoding='utf-8')
    return f'wrote mcp_servers.{plan.name} to {target}'


# -- Public entry point (called by `mesh-mem mcp install` handler) --------------


def install(
    client: MCPClient,
    *,
    name: str = DEFAULT_REGISTRY_NAME,
    extra_env: dict[str, str] | None = None,
    mesh_mem_mcp_path: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Drive a client-specific installer with consistent error semantics."""
    plan = build_install_plan(
        client,
        name=name,
        extra_env=extra_env,
        mesh_mem_mcp_path=mesh_mem_mcp_path,
    )
    if client is MCPClient.CLAUDE_CODE:
        return install_claude_code(plan, force=force, dry_run=dry_run)
    if client is MCPClient.CODEX_CLI:
        return install_codex_cli(plan, force=force, dry_run=dry_run)
    raise ValueError(f'unsupported client: {client!r}')  # pragma: no cover


def parse_env_pairs(pairs: list[str]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` pairs from --env flags; raise on malformed input."""
    out: dict[str, str] = {}
    for raw in pairs:
        if '=' not in raw:
            raise ValueError(f'--env value must be KEY=VALUE: {raw!r}')
        key, _, value = raw.partition('=')
        key = key.strip()
        if not key:
            raise ValueError(f'--env key cannot be empty: {raw!r}')
        out[key] = value
    return out
