"""Tests for `mesh-mem mcp install` (#85).

Both client paths are driven by injecting probes (``which``, subprocess
``run``, config path) so the suite never reaches out to a real Claude Code
or Codex CLI install.
"""

from __future__ import annotations

import copy
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import tomllib
from typing import Callable, NoReturn

import pytest

from kioku_mesh import mcp_install
from kioku_mesh.__main__ import main as cli_main
from kioku_mesh.mcp_install import _build_claude_add_command
from kioku_mesh.mcp_install import _render_codex_toml_block
from kioku_mesh.mcp_install import _repair_identity_env
from kioku_mesh.mcp_install import _replace_codex_block
from kioku_mesh.mcp_install import build_install_plan
from kioku_mesh.mcp_install import DEFAULT_REGISTRY_NAME
from kioku_mesh.mcp_install import install_claude_code
from kioku_mesh.mcp_install import install_codex_cli
from kioku_mesh.mcp_install import InstallPlan
from kioku_mesh.mcp_install import MCPClient
from kioku_mesh.mcp_install import parse_env_pairs
from kioku_mesh.mcp_install import repair
from kioku_mesh.mcp_install import repair_claude_code
from kioku_mesh.mcp_install import repair_codex_cli

# -- parse_env_pairs ------------------------------------------------------------


def test_parse_env_pairs_basic() -> None:
    assert parse_env_pairs(['A=1', 'B=2']) == {'A': '1', 'B': '2'}


def test_parse_env_pairs_preserves_value_equals() -> None:
    assert parse_env_pairs(['A=1=2']) == {'A': '1=2'}


def test_parse_env_pairs_rejects_missing_equals() -> None:
    with pytest.raises(ValueError, match='KEY=VALUE'):
        parse_env_pairs(['nope'])


def test_parse_env_pairs_rejects_empty_key() -> None:
    with pytest.raises(ValueError, match='empty'):
        parse_env_pairs(['=value'])


# -- build_install_plan ---------------------------------------------------------


def test_build_install_plan_defaults_per_client() -> None:
    plan = build_install_plan(MCPClient.CODEX_CLI, kioku_mesh_mcp_path='/x/mesh-mem-mcp')
    assert plan.client is MCPClient.CODEX_CLI
    assert plan.name == DEFAULT_REGISTRY_NAME
    assert plan.command == '/x/mesh-mem-mcp'
    assert plan.env['KIOKU_MESH_AGENT_FAMILY'] == 'codex'
    assert plan.env['KIOKU_MESH_CLIENT_ID'] == 'codex-cli'
    assert plan.env['ZENOH_CONNECT'] == 'tcp/127.0.0.1:7447'


def test_build_install_plan_claude_defaults() -> None:
    plan = build_install_plan(MCPClient.CLAUDE_CODE, kioku_mesh_mcp_path='/x/mesh-mem-mcp')
    assert plan.env['KIOKU_MESH_AGENT_FAMILY'] == 'claude'
    assert plan.env['KIOKU_MESH_CLIENT_ID'] == 'claude-code'


def test_build_install_plan_extra_env_overrides_default() -> None:
    plan = build_install_plan(
        MCPClient.CODEX_CLI,
        kioku_mesh_mcp_path='/x/mesh-mem-mcp',
        extra_env={'KIOKU_MESH_AGENT_FAMILY': 'custom', 'EXTRA': 'value'},
    )
    assert plan.env['KIOKU_MESH_AGENT_FAMILY'] == 'custom'
    assert plan.env['EXTRA'] == 'value'


def test_build_install_plan_raises_when_binary_missing() -> None:
    with pytest.raises(FileNotFoundError, match='kioku-mesh-mcp'):
        build_install_plan(MCPClient.CODEX_CLI, which=lambda _n: None)


@pytest.mark.parametrize(
    'bad_name',
    [
        'foo.bar',  # dot would split a Codex TOML table header
        'has space',  # spaces are not bare keys
        'with"quote',
        'has\\backslash',
        'has@symbol',
        '',
        '\t',
    ],
)
def test_build_install_plan_rejects_unsafe_registry_name(bad_name: str) -> None:
    """Codex review #97: TOML bare keys are [A-Za-z0-9_-]+. Anything else risks a silent rewrite."""
    with pytest.raises(ValueError, match='registry name'):
        build_install_plan(MCPClient.CODEX_CLI, name=bad_name, kioku_mesh_mcp_path='/x/mesh-mem-mcp')


@pytest.mark.parametrize('good_name', ['kioku_mesh', 'mesh-mem', 'foo-bar', 'X42', 'a'])
def test_build_install_plan_accepts_bare_key_names(good_name: str) -> None:
    """All TOML-spec bare keys must be accepted."""
    plan = build_install_plan(MCPClient.CODEX_CLI, name=good_name, kioku_mesh_mcp_path='/x/mesh-mem-mcp')
    assert plan.name == good_name


# -- Claude Code path -----------------------------------------------------------


def test_build_claude_add_command_includes_env_and_command() -> None:
    plan = InstallPlan(
        client=MCPClient.CLAUDE_CODE,
        name='kioku_mesh',
        command='/x/mesh-mem-mcp',
        env={'A': '1', 'B': '2'},
    )
    cmd = _build_claude_add_command('/usr/bin/claude', plan)
    assert cmd[:5] == ['/usr/bin/claude', 'mcp', 'add', 'kioku_mesh', '-s']
    assert '-e' in cmd and 'A=1' in cmd
    assert cmd[-2:] == ['--', '/x/mesh-mem-mcp']


def test_install_claude_code_dry_run_emits_command() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={'A': '1'})
    out = install_claude_code(plan, dry_run=True, which=lambda _n: '/usr/bin/claude')
    assert '/usr/bin/claude mcp add kioku_mesh' in out
    assert '-e A=1' in out


def test_install_claude_code_fresh_register_runs_add() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    msg = install_claude_code(plan, run=fake_run, which=lambda _n: '/usr/bin/claude')
    assert 'registered kioku_mesh' in msg
    # list called first, then add (no remove).
    assert calls[0][1:3] == ['mcp', 'list']
    assert calls[-1][1:4] == ['mcp', 'add', 'kioku_mesh']


def test_install_claude_code_refuses_when_already_registered() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='kioku_mesh: /old/path - Connected\n', stderr='')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    msg = install_claude_code(plan, run=fake_run, which=lambda _n: '/usr/bin/claude')
    assert msg.startswith('error:')
    assert '--force' in msg


def test_install_claude_code_force_removes_then_adds() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    invocations: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        invocations.append(argv[1:])
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='kioku_mesh: /old - Connected\n', stderr='')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    msg = install_claude_code(plan, run=fake_run, force=True, which=lambda _n: '/usr/bin/claude')
    assert 'registered kioku_mesh' in msg
    # Order must be: list -> remove -> add.
    assert invocations[0][:2] == ['mcp', 'list']
    assert invocations[1][:3] == ['mcp', 'remove', 'kioku_mesh']
    assert invocations[2][:3] == ['mcp', 'add', 'kioku_mesh']


def test_install_claude_code_force_raises_when_remove_fails() -> None:
    """Codex review #97: a failed `claude mcp remove` must surface, not get masked by add failure."""
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='kioku_mesh: /old - Connected\n', stderr='')
        if argv[1:3] == ['mcp', 'remove']:
            return subprocess.CompletedProcess(argv, 1, stdout='', stderr='permission denied')
        # Any subsequent call (notably the would-be add) must not happen.
        raise AssertionError(f'unexpected call after failed remove: {argv}')

    with pytest.raises(RuntimeError, match='claude mcp remove kioku_mesh failed'):
        install_claude_code(plan, run=fake_run, force=True, which=lambda _n: '/usr/bin/claude')


def test_install_claude_code_raises_on_subprocess_failure() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:3] == ['mcp', 'list']:
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        return subprocess.CompletedProcess(argv, 1, stdout='', stderr='oops')

    with pytest.raises(RuntimeError, match='claude mcp add failed'):
        install_claude_code(plan, run=fake_run, which=lambda _n: '/usr/bin/claude')


def test_install_claude_code_missing_claude_binary() -> None:
    plan = InstallPlan(client=MCPClient.CLAUDE_CODE, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    with pytest.raises(FileNotFoundError, match='claude binary'):
        install_claude_code(plan, which=lambda _n: None)


# -- Codex CLI path -------------------------------------------------------------


def test_render_codex_toml_block_shape() -> None:
    plan = InstallPlan(
        client=MCPClient.CODEX_CLI,
        name='kioku_mesh',
        command='/x/mesh-mem-mcp',
        env={'A': '1', 'B': '2'},
    )
    block = _render_codex_toml_block(plan)
    assert '[mcp_servers.kioku_mesh]' in block
    assert 'command = "/x/mesh-mem-mcp"' in block
    assert '[mcp_servers.kioku_mesh.env]' in block
    assert 'A = "1"' in block
    assert 'B = "2"' in block


def test_render_codex_toml_block_escapes_quotes_and_backslashes() -> None:
    r"""Values with `"` or `\\` must round-trip as valid TOML, not break the file."""
    plan = InstallPlan(
        client=MCPClient.CODEX_CLI,
        name='kioku_mesh',
        command='/tmp/a"b',
        env={'WINPATH': 'C:\\tools\\bin', 'QUOTED': 'say "hi"'},
    )
    block = _render_codex_toml_block(plan)
    entry = tomllib.loads(block)['mcp_servers']['kioku_mesh']  # must not raise
    assert entry['command'] == '/tmp/a"b'
    assert entry['env'] == {'WINPATH': 'C:\\tools\\bin', 'QUOTED': 'say "hi"'}


def test_install_codex_cli_writes_new_file(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x/mesh-mem-mcp', env={'A': '1'})
    target = tmp_path / 'sub' / 'config.toml'
    msg = install_codex_cli(plan, config_path=target)
    assert target.is_file()
    body = target.read_text()
    assert '[mcp_servers.kioku_mesh]' in body
    assert 'A = "1"' in body
    assert 'wrote mcp_servers.kioku_mesh' in msg


def test_install_codex_cli_appends_to_existing_file(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    target = tmp_path / 'config.toml'
    target.write_text('model = "gpt-5"\n[other_section]\nkey = "value"\n')
    install_codex_cli(plan, config_path=target)
    body = target.read_text()
    # Existing content preserved.
    assert 'model = "gpt-5"' in body
    assert '[other_section]' in body
    # New block appended.
    assert '[mcp_servers.kioku_mesh]' in body


def test_install_codex_cli_refuses_when_already_present(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x/mesh-mem-mcp', env={})
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/old/mesh-mem-mcp"\n\n[mcp_servers.kioku_mesh.env]\nOLD = "1"\n'
    )
    msg = install_codex_cli(plan, config_path=target)
    assert msg.startswith('error:')
    assert '--force' in msg
    # File must NOT have been touched.
    assert '/old/mesh-mem-mcp' in target.read_text()


def test_install_codex_cli_force_replaces_block(tmp_path: Path) -> None:
    plan = InstallPlan(
        client=MCPClient.CODEX_CLI,
        name='kioku_mesh',
        command='/new/mesh-mem-mcp',
        env={'NEW': '1'},
    )
    target = tmp_path / 'config.toml'
    target.write_text(
        'model = "gpt-5"\n\n'
        '[mcp_servers.kioku_mesh]\ncommand = "/old/mesh-mem-mcp"\n\n'
        '[mcp_servers.kioku_mesh.env]\nOLD = "1"\n\n'
        '[mcp_servers.codegraph]\ncommand = "codegraph"\n'
    )
    msg = install_codex_cli(plan, force=True, config_path=target)
    assert 'wrote mcp_servers.kioku_mesh' in msg
    body = target.read_text()
    # Old values gone, new values present.
    assert '/old/mesh-mem-mcp' not in body
    assert '/new/mesh-mem-mcp' in body
    assert 'OLD = "1"' not in body
    assert 'NEW = "1"' in body
    # Unrelated blocks preserved.
    assert 'model = "gpt-5"' in body
    assert '[mcp_servers.codegraph]' in body


def test_install_codex_cli_force_preserves_blocks_before_and_after(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/new/x', env={})
    target = tmp_path / 'config.toml'
    target.write_text(
        '[before]\nbk = "1"\n\n'
        '[mcp_servers.kioku_mesh]\ncommand = "/old"\n'
        '[mcp_servers.kioku_mesh.tools.foo]\napproval_mode = "approve"\n\n'
        '[after]\nak = "2"\n'
    )
    install_codex_cli(plan, force=True, config_path=target)
    body = target.read_text()
    assert '[before]' in body and 'bk = "1"' in body
    assert '[after]' in body and 'ak = "2"' in body
    # Nested-table line from old block should be gone.
    assert 'approval_mode' not in body


def test_install_codex_cli_dry_run_does_not_touch_file(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x', env={})
    target = tmp_path / 'config.toml'
    target.write_text('# existing\n')
    msg = install_codex_cli(plan, dry_run=True, config_path=target)
    assert 'would write to' in msg
    assert '[mcp_servers.kioku_mesh]' in msg
    assert target.read_text() == '# existing\n'  # untouched


def test_install_codex_cli_rejects_unparseable_toml(tmp_path: Path) -> None:
    plan = InstallPlan(client=MCPClient.CODEX_CLI, name='kioku_mesh', command='/x', env={})
    target = tmp_path / 'config.toml'
    target.write_text('garbage = [not, closed,\n')
    with pytest.raises(RuntimeError, match='cannot parse'):
        install_codex_cli(plan, config_path=target)


# -- _replace_codex_block direct tests -----------------------------------------


def test_replace_codex_block_no_existing_appends() -> None:
    existing = 'model = "gpt-5"\n'
    new = _replace_codex_block(existing, 'kioku_mesh', '[mcp_servers.kioku_mesh]\ncommand = "/x"')
    assert 'model = "gpt-5"' in new
    assert '[mcp_servers.kioku_mesh]' in new
    assert new.count('[mcp_servers.kioku_mesh]') == 1


def test_replace_codex_block_only_replaces_matching_name() -> None:
    existing = (
        '[mcp_servers.other]\ncommand = "/other"\n\n'
        '[mcp_servers.kioku_mesh]\ncommand = "/old"\n\n'
        '[mcp_servers.kioku_mesh.env]\nA = "1"\n'
    )
    new = _replace_codex_block(existing, 'kioku_mesh', '[mcp_servers.kioku_mesh]\ncommand = "/new"')
    assert '/other' in new
    assert '/new' in new
    assert '/old' not in new
    assert 'A = "1"' not in new  # nested table swept


# -- _repair_identity_env --------------------------------------------------------


def test_repair_identity_env_renames_legacy_key_without_current_twin() -> None:
    env = {'ZENOH_CONNECT': 'tcp/127.0.0.1:7447', 'MESH_MEM_AGENT_FAMILY': 'claude'}
    repaired = _repair_identity_env(env)
    assert repaired == {'ZENOH_CONNECT': 'tcp/127.0.0.1:7447', 'KIOKU_MESH_AGENT_FAMILY': 'claude'}


def test_repair_identity_env_renames_both_identity_keys() -> None:
    env = {'MESH_MEM_AGENT_FAMILY': 'codex', 'MESH_MEM_CLIENT_ID': 'codex-cli'}
    repaired = _repair_identity_env(env)
    assert repaired == {'KIOKU_MESH_AGENT_FAMILY': 'codex', 'KIOKU_MESH_CLIENT_ID': 'codex-cli'}


def test_repair_identity_env_leaves_unrelated_user_env_untouched() -> None:
    env = {'MY_CUSTOM_FLAG': 'on', 'ANOTHER_SETTING': '42'}
    assert _repair_identity_env(env) == env


def test_repair_identity_env_no_op_when_current_prefix_already_present() -> None:
    """A config already carrying both names is correct; repair keeps the inert legacy key."""
    env = {'MESH_MEM_AGENT_FAMILY': 'claude', 'KIOKU_MESH_AGENT_FAMILY': 'claude'}
    assert _repair_identity_env(env) == env


def test_repair_identity_env_no_op_when_nothing_legacy() -> None:
    env = {'KIOKU_MESH_AGENT_FAMILY': 'claude', 'KIOKU_MESH_CLIENT_ID': 'claude-code'}
    assert _repair_identity_env(env) == env


# -- repair_codex_cli -------------------------------------------------------------


def test_repair_codex_cli_renames_legacy_prefix_preserves_command_and_extra_env(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    target.write_text(
        'model = "gpt-5"\n\n'
        '[mcp_servers.kioku_mesh]\n'
        'command = "/home/user/.local/bin/kioku-mesh-mcp"\n\n'
        '[mcp_servers.kioku_mesh.env]\n'
        'ZENOH_CONNECT = "tcp/127.0.0.1:7447"\n'
        'MESH_MEM_AGENT_FAMILY = "codex"\n'
        'MESH_MEM_CLIENT_ID = "codex-cli"\n'
        'MY_CUSTOM_TIMEOUT = "30"\n\n'
        '[mcp_servers.other]\n'
        'command = "/other"\n'
    )
    msg = repair_codex_cli(config_path=target)
    assert 'repaired' in msg
    body = target.read_text()
    assert 'MESH_MEM_AGENT_FAMILY' not in body
    assert 'MESH_MEM_CLIENT_ID' not in body
    assert 'KIOKU_MESH_AGENT_FAMILY = "codex"' in body
    assert 'KIOKU_MESH_CLIENT_ID = "codex-cli"' in body
    # Unrelated user env / command / other blocks untouched.
    assert 'MY_CUSTOM_TIMEOUT = "30"' in body
    assert 'command = "/home/user/.local/bin/kioku-mesh-mcp"' in body
    assert 'model = "gpt-5"' in body
    assert '[mcp_servers.other]' in body


def test_repair_codex_cli_no_op_when_already_current_prefix(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    original = (
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\n\n[mcp_servers.kioku_mesh.env]\nKIOKU_MESH_AGENT_FAMILY = "codex"\n'
    )
    target.write_text(original)
    msg = repair_codex_cli(config_path=target)
    assert 'nothing to repair' in msg
    assert target.read_text() == original  # untouched


def test_repair_codex_cli_errors_when_entry_missing(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    target.write_text('model = "gpt-5"\n')
    msg = repair_codex_cli(config_path=target)
    assert msg.startswith('error:')
    assert 'kioku_mesh' in msg


def test_repair_codex_cli_errors_when_file_missing(tmp_path: Path) -> None:
    target = tmp_path / 'missing.toml'
    msg = repair_codex_cli(config_path=target)
    assert msg.startswith('error:')


def test_repair_codex_cli_preserves_other_fields_and_comments_in_target_block(tmp_path: Path) -> None:
    """Only the identity key tokens change — args / enabled / timeout / comments stay."""
    target = tmp_path / 'config.toml'
    original = (
        '[mcp_servers.kioku_mesh]\n'
        'command = "/home/user/.local/bin/kioku-mesh-mcp"\n'
        'args = ["--verbose", "--flag"]\n'
        'enabled = true\n'
        'startup_timeout_sec = 45\n'
        '\n'
        '# keep this note about the env block\n'
        '[mcp_servers.kioku_mesh.env]\n'
        'MESH_MEM_AGENT_FAMILY = "codex"  # trailing comment\n'
        'MY_CUSTOM_TIMEOUT = "30"\n'
    )
    target.write_text(original)

    msg = repair_codex_cli(config_path=target)

    assert 'repaired' in msg
    body = target.read_text()
    assert 'args = ["--verbose", "--flag"]' in body
    assert 'enabled = true' in body
    assert 'startup_timeout_sec = 45' in body
    assert '# keep this note about the env block' in body
    assert 'KIOKU_MESH_AGENT_FAMILY = "codex"  # trailing comment' in body
    assert 'MESH_MEM_AGENT_FAMILY' not in body
    entry = tomllib.loads(body)['mcp_servers']['kioku_mesh']
    assert entry['args'] == ['--verbose', '--flag']
    assert entry['enabled'] is True
    assert entry['startup_timeout_sec'] == 45
    assert entry['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'codex', 'MY_CUSTOM_TIMEOUT': '30'}


def test_repair_codex_cli_keeps_quoted_values_valid_toml(tmp_path: Path) -> None:
    """Values that would need escaping survive because nothing is re-rendered."""
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\n'
        "command = '/tmp/a\"b'\n"
        '\n'
        '[mcp_servers.kioku_mesh.env]\n'
        "KEEP = 'a\"b'\n"
        'WINPATH = "C:\\\\tools\\\\bin"\n'
        'MESH_MEM_CLIENT_ID = "codex-cli"\n'
    )

    msg = repair_codex_cli(config_path=target)

    assert 'repaired' in msg
    entry = tomllib.loads(target.read_text())['mcp_servers']['kioku_mesh']  # must not raise
    assert entry['command'] == '/tmp/a"b'
    assert entry['env'] == {'KEEP': 'a"b', 'WINPATH': 'C:\\tools\\bin', 'KIOKU_MESH_CLIENT_ID': 'codex-cli'}


@pytest.mark.parametrize('quote', ['"', "'"])
def test_repair_codex_cli_keeps_the_quote_style_of_a_quoted_identity_key(quote: str, tmp_path: Path) -> None:
    """NB1: a quoted key token stays quoted — only the name inside it changes."""
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\n'
        'command = "/home/user/.local/bin/kioku-mesh-mcp"\n'
        '\n'
        '[mcp_servers.kioku_mesh.env]\n'
        f'{quote}MESH_MEM_AGENT_FAMILY{quote} = "codex"\n'
    )

    repair_codex_cli(config_path=target)

    body = target.read_text()
    assert f'{quote}KIOKU_MESH_AGENT_FAMILY{quote} = "codex"' in body
    assert tomllib.loads(body)['mcp_servers']['kioku_mesh']['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'codex'}


@pytest.mark.parametrize('quote', ['"', "'"])
def test_repair_codex_cli_keeps_the_quote_style_of_a_quoted_inline_key(quote: str, tmp_path: Path) -> None:
    """NB1: same contract for the one-line ``env = { ... }`` layout."""
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\n'
        'command = "/home/user/.local/bin/kioku-mesh-mcp"\n'
        f'env = {{ {quote}MESH_MEM_CLIENT_ID{quote} = "codex-cli" }}\n'
    )

    repair_codex_cli(config_path=target)

    body = target.read_text()
    assert f'{quote}KIOKU_MESH_CLIENT_ID{quote} = "codex-cli"' in body
    assert tomllib.loads(body)['mcp_servers']['kioku_mesh']['env'] == {'KIOKU_MESH_CLIENT_ID': 'codex-cli'}


def test_repair_codex_cli_renames_key_inside_inline_env_table(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\nenv = { MESH_MEM_AGENT_FAMILY = "codex", OTHER = "1" }\n'
    )

    msg = repair_codex_cli(config_path=target)

    assert 'repaired' in msg
    body = target.read_text()
    assert body.endswith('env = { KIOKU_MESH_AGENT_FAMILY = "codex", OTHER = "1" }\n')
    assert tomllib.loads(body)['mcp_servers']['kioku_mesh']['env'] == {
        'KIOKU_MESH_AGENT_FAMILY': 'codex',
        'OTHER': '1',
    }


def test_repair_codex_cli_fails_closed_on_layout_it_cannot_edit(tmp_path: Path) -> None:
    """An entry with no table header of its own is refused, leaving the file alone."""
    target = tmp_path / 'config.toml'
    original = '[mcp_servers]\nkioku_mesh = { command = "/x", env = { MESH_MEM_AGENT_FAMILY = "codex" } }\n'
    target.write_text(original)

    with pytest.raises(RuntimeError, match='unsupported layout'):
        repair_codex_cli(config_path=target)
    assert target.read_text() == original


# -- repair_claude_code (direct edit of Claude Code's MCP config JSON) --------------
#
# The previous implementation shelled out to `claude mcp get` and parsed its
# human-readable output. That route is lossy by construction (see the module
# docstring), so these tests drive the JSON store the CLI itself writes.


def _claude_entry(
    *,
    command: str = '/home/user/.local/bin/kioku-mesh-mcp',
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    **extra: object,
) -> dict[str, object]:
    """Build one ``mcpServers`` entry as Claude Code stores it."""
    entry: dict[str, object] = {
        'type': 'stdio',
        'command': command,
        'args': args if args is not None else [],
        'env': env if env is not None else {'MESH_MEM_AGENT_FAMILY': 'claude'},
    }
    entry.update(extra)
    return entry


def _write_claude_config(path: Path, document: dict[str, object], *, indent: int | None = 2) -> None:
    path.write_text(json.dumps(document, indent=indent) + '\n', encoding='utf-8')


def _user_scope_config(path: Path, entry: dict[str, object] | None = None, **top_level: object) -> None:
    document: dict[str, object] = {'mcpServers': {DEFAULT_REGISTRY_NAME: entry or _claude_entry()}}
    document.update(top_level)
    _write_claude_config(path, document)


def test_repair_claude_code_renames_identity_env_in_user_scope(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    _user_scope_config(
        config,
        _claude_entry(
            env={
                'ZENOH_CONNECT': 'tcp/127.0.0.1:7447',
                'MESH_MEM_AGENT_FAMILY': 'claude',
                'MESH_MEM_CLIENT_ID': 'claude-code',
            }
        ),
    )

    msg = repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert 'repaired' in msg
    entry = json.loads(config.read_text())['mcpServers'][DEFAULT_REGISTRY_NAME]
    assert entry['env'] == {
        'ZENOH_CONNECT': 'tcp/127.0.0.1:7447',
        'KIOKU_MESH_AGENT_FAMILY': 'claude',
        'KIOKU_MESH_CLIENT_ID': 'claude-code',
    }
    # Key order is positional, not just set-equal: the renamed keys stay put.
    assert list(entry['env']) == ['ZENOH_CONNECT', 'KIOKU_MESH_AGENT_FAMILY', 'KIOKU_MESH_CLIENT_ID']


def test_repair_claude_code_preserves_args_containing_spaces(tmp_path: Path) -> None:
    """The text-parse route space-joined ``Args:`` and silently split them back apart.

    ``claude mcp get`` prints ``Args: --flag two words`` for
    ``["--flag", "two words"]``, so any repair that re-registers from that
    output rewrites the user's registration. Reading the JSON keeps the list.
    """
    config = tmp_path / '.claude.json'
    args = ['--flag', 'two words', '', '--json={"a": 1}']
    _user_scope_config(config, _claude_entry(args=args))

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    entry = json.loads(config.read_text())['mcpServers'][DEFAULT_REGISTRY_NAME]
    assert entry['args'] == args
    assert entry['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'claude'}


def test_repair_claude_code_preserves_env_values_the_text_route_mangled(tmp_path: Path) -> None:
    """Multi-line values, trailing whitespace and trailing colons survive verbatim."""
    config = tmp_path / '.claude.json'
    hostile = {
        'MULTI': 'line one\n- bullet shaped\nMetadata: x',
        'SPACED': '  leading and trailing  ',
        'KEEP': 'ends-with-colon:',
        'MESH_MEM_CLIENT_ID': 'claude-code',
    }
    _user_scope_config(config, _claude_entry(env=dict(hostile)))

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    env = json.loads(config.read_text())['mcpServers'][DEFAULT_REGISTRY_NAME]['env']
    assert env['MULTI'] == hostile['MULTI']
    assert env['SPACED'] == hostile['SPACED']
    assert env['KEEP'] == hostile['KEEP']
    assert env['KIOKU_MESH_CLIENT_ID'] == 'claude-code'
    assert 'MESH_MEM_CLIENT_ID' not in env


def test_repair_claude_code_leaves_everything_outside_the_identity_env_untouched(tmp_path: Path) -> None:
    """Only the two identity keys move; every other byte of meaning is preserved."""
    config = tmp_path / '.claude.json'
    original = {
        'numStartups': 42,
        'mcpServers': {
            'other-server': _claude_entry(command='/usr/bin/other', env={'MESH_MEM_AGENT_FAMILY': 'nope'}),
            DEFAULT_REGISTRY_NAME: _claude_entry(
                args=['--verbose'],
                env={'MESH_MEM_AGENT_FAMILY': 'claude', 'UNRELATED': 'keep me'},
                # A field this version of kioku-mesh knows nothing about.
                futureField={'nested': [1, 2, {'deep': True}]},
            ),
        },
        'projects': {'/somewhere/else': {'allowedTools': ['Bash']}},
    }
    _write_claude_config(config, original)

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    written = json.loads(config.read_text())
    expected = copy.deepcopy(original)
    expected['mcpServers'][DEFAULT_REGISTRY_NAME]['env'] = {
        'KIOKU_MESH_AGENT_FAMILY': 'claude',
        'UNRELATED': 'keep me',
    }
    assert written == expected
    # A different server's legacy env is *not* ours to fix.
    assert written['mcpServers']['other-server']['env'] == {'MESH_MEM_AGENT_FAMILY': 'nope'}
    assert list(written) == list(original)  # top-level key order preserved


def test_repair_claude_code_repairs_local_scope_entry(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    project = tmp_path / 'proj'
    _write_claude_config(
        config, {'projects': {str(project): {'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry()}}}}
    )

    msg = repair_claude_code(config_path=config, project_dir=project)

    assert 'local scope' in msg
    servers = json.loads(config.read_text())['projects'][str(project)]['mcpServers']
    assert servers[DEFAULT_REGISTRY_NAME]['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'claude'}


def test_repair_claude_code_repairs_project_scope_entry(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    config.write_text('{}\n')
    project = tmp_path / 'proj'
    project.mkdir()
    mcp_json = project / '.mcp.json'
    _write_claude_config(mcp_json, {'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry()}})

    msg = repair_claude_code(config_path=config, project_dir=project)

    assert 'project scope' in msg
    assert str(mcp_json) in msg
    servers = json.loads(mcp_json.read_text())['mcpServers']
    assert servers[DEFAULT_REGISTRY_NAME]['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'claude'}


def test_repair_claude_code_fails_closed_when_registered_in_several_scopes(tmp_path: Path) -> None:
    """Two scopes, no way to know which one the user meant: refuse and change nothing."""
    config = tmp_path / '.claude.json'
    project = tmp_path / 'proj'
    project.mkdir()
    _write_claude_config(
        config,
        {
            'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry(command='/user/scope')},
            'projects': {str(project): {'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry(command='/local/scope')}}},
        },
    )
    mcp_json = project / '.mcp.json'
    _write_claude_config(mcp_json, {'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry(command='/project/scope')}})
    before = (config.read_text(), mcp_json.read_text())

    with pytest.raises(RuntimeError) as excinfo:
        repair_claude_code(config_path=config, project_dir=project)

    message = str(excinfo.value)
    assert 'registered in 3 scopes' in message
    for scope in ('local scope', 'user scope', 'project scope'):
        assert scope in message  # every candidate is named...
    assert str(config) in message and str(mcp_json) in message  # ...with the file holding it
    assert f'claude mcp remove {DEFAULT_REGISTRY_NAME} -s <scope>' in message  # actionable next step
    assert (config.read_text(), mcp_json.read_text()) == before  # nothing written
    assert not list(tmp_path.glob('**/*.bak-*'))  # not even a backup


def test_repair_claude_code_fails_closed_on_two_scopes_even_when_only_one_needs_repair(tmp_path: Path) -> None:
    """The ambiguity is which registration is *the* one, not which one is broken."""
    config = tmp_path / '.claude.json'
    project = tmp_path / 'proj'
    _write_claude_config(
        config,
        {
            'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry(env={'KIOKU_MESH_AGENT_FAMILY': 'claude'})},
            'projects': {str(project): {'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry()}}},
        },
    )
    before = config.read_text()

    with pytest.raises(RuntimeError, match='registered in 2 scopes'):
        repair_claude_code(config_path=config, project_dir=project)
    assert config.read_text() == before


def test_repair_claude_code_no_op_when_already_current_prefix(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    _user_scope_config(config, _claude_entry(env={'KIOKU_MESH_AGENT_FAMILY': 'claude'}))
    before = config.read_text()

    msg = repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert 'nothing to repair' in msg
    assert config.read_text() == before
    assert not list(tmp_path.glob('*.bak-*'))  # a no-op writes nothing at all


def test_repair_claude_code_errors_when_not_registered(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    _write_claude_config(config, {'mcpServers': {'someone-else': _claude_entry()}})

    msg = repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert msg.startswith('error:')
    assert str(config) in msg
    assert 'mcp install --client claude-code' in msg


def test_repair_claude_code_errors_when_config_missing(tmp_path: Path) -> None:
    msg = repair_claude_code(config_path=tmp_path / 'absent.json', project_dir=tmp_path / 'proj')
    assert msg.startswith('error:')


def test_repair_claude_code_rejects_unparseable_config(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    config.write_text('{"mcpServers": ')

    with pytest.raises(RuntimeError, match='cannot parse'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')


def test_repair_claude_code_rejects_env_that_is_not_a_string_mapping(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    _user_scope_config(config, _claude_entry(env=None) | {'env': ['MESH_MEM_AGENT_FAMILY=claude']})
    before = config.read_text()

    with pytest.raises(RuntimeError, match='not a mapping of strings'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')
    assert config.read_text() == before


def test_repair_claude_code_keeps_a_backup_of_the_previous_file(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    before = config.read_text()

    msg = repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    backups = list(tmp_path.glob('.claude.json.bak-*'))
    assert len(backups) == 1
    assert backups[0].read_text() == before  # byte-for-byte, before any edit
    assert str(backups[0]) in msg
    assert not list(tmp_path.glob('*.tmp'))  # the atomic-write temp file is gone


def test_repair_claude_code_does_not_clobber_a_hand_made_backup(tmp_path: Path) -> None:
    """``~/.claude.json.bak`` is a real thing people have; ours must not overwrite it."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    hand_made = tmp_path / '.claude.json.bak'
    hand_made.write_text('the user made this months ago')

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert hand_made.read_text() == 'the user made this months ago'
    assert len(list(tmp_path.glob('.claude.json.bak-*'))) == 1


def test_repair_claude_code_preserves_the_files_own_indentation(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    _write_claude_config(config, {'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry()}}, indent=4)

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    text = config.read_text()
    assert '\n    "mcpServers"' in text
    assert text.endswith('\n')


def test_repair_claude_code_keeps_a_minified_config_minified(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    config.write_text(json.dumps({'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry()}}))

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    text = config.read_text()
    assert '\n' not in text
    assert 'KIOKU_MESH_AGENT_FAMILY' in text


def _patch_rendered_text(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Make the in-place editor produce ``text``, to drive the write guards."""
    monkeypatch.setattr(
        mcp_install,
        '_rename_claude_env_keys',
        lambda raw_text, *, container, name, renames: text,
    )


def test_repair_claude_code_refuses_to_write_content_that_does_not_verify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Validation happens on the staged file, so a wrong edit never lands at all."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    before = config.read_text()
    _patch_rendered_text(monkeypatch, json.dumps({'mcpServers': {}}))

    with pytest.raises(RuntimeError, match='does not match the intended document'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before  # never replaced in the first place
    assert not list(tmp_path.glob('.claude.json.bak-*'))  # and no half-finished backup left behind
    assert not list(tmp_path.glob('*.tmp'))


def test_repair_claude_code_refuses_to_write_content_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    before = config.read_text()
    _patch_rendered_text(monkeypatch, '{"truncated"')

    with pytest.raises(RuntimeError, match='not strict JSON'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before
    assert not list(tmp_path.glob('*.tmp'))


def test_repair_claude_code_restores_the_backup_when_the_landed_file_is_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read-back is still the last guarantee: our own bad write is rolled back.

    The staged-file validation makes this unreachable in practice, which is
    exactly why it is worth pinning: dropping the read-back must stay visible.
    """
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    before = config.read_text()
    _patch_rendered_text(monkeypatch, json.dumps({'mcpServers': {}}))
    monkeypatch.setattr(
        mcp_install,
        '_validate_pending_write',
        lambda tmp, new_text, expected, *, destination: None,  # pretend the staged check passed
    )

    with pytest.raises(RuntimeError, match='does not match the intended one'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before  # the bad write was rolled back


def test_repair_claude_code_leaves_a_third_party_rewrite_alone_after_the_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A newer config from another writer must not be clobbered by a stale backup."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    newer = json.dumps({'mcpServers': {}, 'writtenBy': 'claude code itself'})

    def steal(directory: Path) -> None:  # runs right after os.replace
        config.write_text(newer, encoding='utf-8')

    monkeypatch.setattr(mcp_install, '_fsync_directory', steal)

    with pytest.raises(RuntimeError, match='changed again right after'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == newer  # left as the other writer left it
    assert len(list(tmp_path.glob('.claude.json.bak-*'))) == 1  # the pre-repair copy is still there


def test_repair_claude_code_swaps_the_file_in_rather_than_writing_over_it(tmp_path: Path) -> None:
    """A temp file + ``os.replace`` gives the destination a new inode.

    Writing in place (``path.write_text``) keeps the inode and exposes a
    window where a reader sees a truncated config, so the inode change is the
    observable proof the write was atomic.
    """
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    inode_before = config.stat().st_ino

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.stat().st_ino != inode_before


def test_repair_claude_code_leaves_the_original_in_place_when_the_write_blows_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """os.replace lands the new file in one step: a mid-write failure can't truncate."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    before = config.read_text()

    def boom(raw_text: str, *, container: object, name: str, renames: object) -> str:
        raise OSError('disk full')

    monkeypatch.setattr(mcp_install, '_rename_claude_env_keys', boom)

    with pytest.raises(OSError, match='disk full'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before
    assert not list(tmp_path.glob('*.tmp'))


# -- repair (Claude Code): write-safety contracts ------------------------------
#
# Everything below pins a way the previous whole-document rewrite could damage a
# config the user did not ask --repair to touch (Codex review B1-B5 on PR #287).


def _minimal_document(env: dict[str, str] | None = None) -> str:
    """One valid config as raw text, formatted the way this module must preserve."""
    entry = json.dumps(
        {'type': 'stdio', 'command': '/x', 'env': env or {'MESH_MEM_AGENT_FAMILY': 'claude'}},
        indent=2,
    )
    return '{\n  "mcpServers": {\n    "' + DEFAULT_REGISTRY_NAME + '": ' + entry + '\n  }\n}\n'


def test_repair_claude_code_keeps_a_number_python_would_widen_to_infinity(tmp_path: Path) -> None:
    """``1e400`` is valid JSON; ``Infinity`` is not, and Claude Code rejects it.

    ``json.loads`` turns the literal into ``float('inf')`` and ``json.dumps``
    writes it back as the non-standard ``Infinity`` token, at which point the
    real CLI reports the config as corrupted. Editing the key token in place
    never touches the number (Codex review B1).
    """
    config = tmp_path / '.claude.json'
    before = _minimal_document().replace('{\n  "mcpServers"', '{\n  "futureNumeric": 1e400,\n  "mcpServers"')
    config.write_text(before, encoding='utf-8')

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    after = config.read_text()
    assert '1e400' in after
    assert 'Infinity' not in after
    assert after == before.replace('"MESH_MEM_AGENT_FAMILY"', '"KIOKU_MESH_AGENT_FAMILY"')


def test_repair_claude_code_fails_closed_on_a_config_that_is_already_non_standard(tmp_path: Path) -> None:
    """A config Claude Code itself cannot read is not something --repair may edit."""
    config = tmp_path / '.claude.json'
    before = _minimal_document().replace('{\n  "mcpServers"', '{\n  "weird": Infinity,\n  "mcpServers"')
    config.write_text(before, encoding='utf-8')

    with pytest.raises(RuntimeError, match='cannot parse'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before


def test_repair_claude_code_fails_closed_when_the_config_changes_before_the_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Claude Code rewrites this file while it runs; a lost update must not pass as success."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    real_rename = mcp_install._rename_claude_env_keys

    def concurrent_writer(raw_text: str, *, container: object, name: str, renames: object) -> str:
        document = json.loads(config.read_text())
        document['concurrentClaudeUpdate'] = {'must': 'survive'}
        config.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
        return real_rename(raw_text, container=container, name=name, renames=renames)

    monkeypatch.setattr(mcp_install, '_rename_claude_env_keys', concurrent_writer)

    with pytest.raises(RuntimeError, match='changed on disk'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    written = json.loads(config.read_text())
    assert written['concurrentClaudeUpdate'] == {'must': 'survive'}  # not dropped
    assert not list(tmp_path.glob('.claude.json.bak-*'))
    assert not list(tmp_path.glob('*.tmp'))


def test_repair_claude_code_fails_closed_when_the_config_changes_while_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same check runs again immediately before the replace, not only once."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    real_backup = mcp_install._write_backup

    def concurrent_writer(backup: Path, text: str, *, mode: int) -> None:
        real_backup(backup, text, mode=mode)
        document = json.loads(config.read_text())
        document['concurrentClaudeUpdate'] = {'must': 'survive'}
        config.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')

    monkeypatch.setattr(mcp_install, '_write_backup', concurrent_writer)

    with pytest.raises(RuntimeError, match='changed on disk'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert json.loads(config.read_text())['concurrentClaudeUpdate'] == {'must': 'survive'}
    assert not list(tmp_path.glob('.claude.json.bak-*'))  # the useless backup is cleaned up
    assert not list(tmp_path.glob('*.tmp'))


def test_repair_claude_code_keeps_a_symlinked_config_a_symlink(tmp_path: Path) -> None:
    """``os.replace`` on the link would detach the config from the file it names."""
    real = tmp_path / 'real.json'
    _user_scope_config(real)
    config = tmp_path / '.claude.json'
    config.symlink_to(real)

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.is_symlink()
    assert os.readlink(config) == str(real)
    env = json.loads(real.read_text())['mcpServers'][DEFAULT_REGISTRY_NAME]['env']
    assert env == {'KIOKU_MESH_AGENT_FAMILY': 'claude'}  # the referent is what changed
    assert [p.name for p in tmp_path.glob('*.bak-*')] == [p.name for p in tmp_path.glob('real.json.bak-*')]


def test_resolve_config_target_refuses_a_link_to_something_that_is_not_a_file(tmp_path: Path) -> None:
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    link = tmp_path / '.claude.json'
    link.symlink_to(fifo)

    with pytest.raises(RuntimeError, match='not a regular file'):
        mcp_install._resolve_config_target(link)


def _repair_under_umask(config: Path, project_dir: Path, umask: int) -> None:
    previous = os.umask(umask)
    try:
        repair_claude_code(config_path=config, project_dir=project_dir)
    finally:
        os.umask(previous)


def test_repair_claude_code_never_makes_the_backup_more_readable_than_the_config(tmp_path: Path) -> None:
    """A 0600 config holds MCP env secrets; a umask-wide 0664 backup leaks them (B4)."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    config.chmod(0o600)

    _repair_under_umask(config, tmp_path / 'proj', 0o002)

    (backup,) = tmp_path.glob('.claude.json.bak-*')
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.stat().st_mode) == 0o600  # and the config keeps its own mode


def test_repair_claude_code_preserves_a_group_readable_config_mode(tmp_path: Path) -> None:
    """The other direction: a 0664 project config must not silently become 0600."""
    config = tmp_path / '.mcp.json'
    config.write_text(json.dumps({'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry()}}, indent=2) + '\n')
    config.chmod(0o664)

    _repair_under_umask(tmp_path / '.claude.json', tmp_path, 0o002)  # project scope lives in tmp_path

    assert stat.S_IMODE(config.stat().st_mode) == 0o664
    (backup,) = tmp_path.glob('.mcp.json.bak-*')
    assert stat.S_IMODE(backup.stat().st_mode) & 0o077 == 0  # backup stays owner-only


_HOSTILE_LAYOUT = (
    '{\n'
    '\t"numStartups":42,\n'
    '\t"mcpServers":{"' + DEFAULT_REGISTRY_NAME + '":{"command":"/usr/bin/x","args":["--a","b c"],'
    '"env":{"KEEP":"a\\/b \\u00e9","MESH_MEM_AGENT_FAMILY":"claude","MESH_MEM_CLIENT_ID":"claude-code"}}},\n'
    '\t"projects" : { "/p" : { "allowedTools" : [ ] } }\n'
    '}'
)


def test_repair_claude_code_changes_nothing_but_the_two_identity_key_tokens(tmp_path: Path) -> None:
    """The byte-level contract: tab indent, compact containers, spacing and escapes all stay.

    Re-serializing the parsed document normalized every one of these, which is
    not what "leave everything else alone" means (Codex review B5).
    """
    config = tmp_path / '.claude.json'
    config.write_text(_HOSTILE_LAYOUT, encoding='utf-8')

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    after = config.read_text()
    expected = _HOSTILE_LAYOUT.replace('"MESH_MEM_AGENT_FAMILY"', '"KIOKU_MESH_AGENT_FAMILY"').replace(
        '"MESH_MEM_CLIENT_ID"', '"KIOKU_MESH_CLIENT_ID"'
    )
    assert after == expected
    assert r'a\/b \u00e9' in after  # escape spelling is the file's, not json.dumps'
    assert not after.endswith('\n')  # including the missing trailing newline


def test_repair_claude_code_validates_the_new_file_before_it_lands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Crash between replace and read-back: whatever is live must already be checked."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)

    def crash(path: Path, expected: dict[str, object], *, backup: Path, written_text: str) -> None:
        raise KeyboardInterrupt('power cut right after the replace')

    monkeypatch.setattr(mcp_install, '_verify_written_document', crash)

    with pytest.raises(KeyboardInterrupt):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    entry = json.loads(config.read_text())['mcpServers'][DEFAULT_REGISTRY_NAME]
    assert entry['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'claude'}


def test_repair_claude_code_fsyncs_the_directory_so_the_new_name_survives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the directory fsync the rename itself can be lost on a power cut."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    real_fsync = os.fsync
    fsynced_a_directory = []

    def record(fd: int) -> None:
        fsynced_a_directory.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        real_fsync(fd)

    monkeypatch.setattr(os, 'fsync', record)

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert any(fsynced_a_directory)


def _raise_oserror(code: int, message: str) -> Callable[..., NoReturn]:
    """Build a stand-in that always fails with ``code``, whatever it is called with."""

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(code, message)

    return fail


def _os_with_failing_replace() -> object:
    """Build a view of ``os`` whose ``replace`` fails, to drive the nothing-landed path."""

    class _FailingReplace:
        def __getattr__(self, name: str) -> object:
            return getattr(os, name)

        def replace(self, *_args: object, **_kwargs: object) -> NoReturn:
            raise OSError(errno.EIO, 'simulated replace failure')

    return _FailingReplace()


def _require_user_xattrs(path: Path) -> None:
    """Skip when the filesystem under test has no user xattrs (tmpfs mounts, some CI images)."""
    try:
        os.setxattr(path, 'user.kioku_probe', b'1')
    except (AttributeError, OSError) as e:
        pytest.skip(f'filesystem does not support user extended attributes: {e}')
    os.removexattr(path, 'user.kioku_probe')


def _sample_posix_acl(tmp_path: Path) -> bytes:
    """Put a named-user ACL on ``.claude.json`` and return its raw xattr bytes.

    Built by copying a donor file's ACL rather than shelling out to ``setfacl``,
    which is not installed everywhere.
    """
    donor = tmp_path / 'acl-donor'
    donor.write_text('x', encoding='utf-8')
    config = tmp_path / '.claude.json'
    # A minimal POSIX.1e ACL: version 2 header then (tag, perm, id) triples for
    # user_obj rw-, named user 0 r--, group_obj r--, mask rw-, other r--.
    acl = (
        b'\x02\x00\x00\x00'
        b'\x01\x00\x06\x00\xff\xff\xff\xff'
        b'\x02\x00\x04\x00\x00\x00\x00\x00'
        b'\x04\x00\x04\x00\xff\xff\xff\xff'
        b'\x10\x00\x06\x00\xff\xff\xff\xff'
        b'\x20\x00\x04\x00\xff\xff\xff\xff'
    )
    try:
        os.setxattr(config, 'system.posix_acl_access', acl)
    except OSError as e:
        pytest.skip(f'filesystem does not support POSIX ACLs: {e}')
    return os.getxattr(config, 'system.posix_acl_access')


def test_repair_claude_code_keeps_the_backup_when_the_directory_fsync_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A post-replace failure must not delete the only pre-repair copy (Codex review on #287).

    The broad handler used to unlink the backup even after ``os.replace`` had
    succeeded, so a directory-fsync error left the repaired config live with
    nothing to roll back to.
    """
    config = tmp_path / '.claude.json'
    _user_scope_config(config)

    def boom(directory: Path) -> None:
        raise OSError(errno.EIO, 'simulated directory fsync failure')

    monkeypatch.setattr(mcp_install, '_fsync_directory', boom)

    with pytest.raises(RuntimeError, match='not durably confirmed'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    # The replace itself succeeded, so what is live is the repaired document...
    after = config.read_text()
    assert 'KIOKU_MESH_AGENT_FAMILY' in after
    assert 'MESH_MEM_AGENT_FAMILY' not in after
    # ...and the pre-repair bytes are still recoverable.
    (backup,) = tmp_path.glob('.claude.json.bak-*')
    assert 'MESH_MEM_AGENT_FAMILY' in backup.read_text()
    assert not list(tmp_path.glob('.claude.json.*.tmp'))


def test_repair_claude_code_names_the_kept_backup_when_durability_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The error has to say the config is live and where the old one is, or the backup is useless."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    monkeypatch.setattr(
        mcp_install, '_fsync_directory', _raise_oserror(errno.EIO, 'simulated directory fsync failure')
    )

    with pytest.raises(RuntimeError) as excinfo:
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    (backup,) = tmp_path.glob('.claude.json.bak-*')
    message = str(excinfo.value)
    assert str(backup) in message
    assert 'now holds the repaired content' in message


def test_repair_claude_code_still_cleans_up_the_backup_when_nothing_landed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other side of the same rule: a pre-replace failure leaves no litter behind."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    before = config.read_text()
    monkeypatch.setattr(mcp_install, 'os', _os_with_failing_replace())

    with pytest.raises(OSError):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before
    assert not list(tmp_path.glob('.claude.json.bak-*'))
    assert not list(tmp_path.glob('.claude.json.*.tmp'))


def test_repair_claude_code_preserves_extended_attributes(tmp_path: Path) -> None:
    """``os.replace`` hands the destination the staged file's xattrs, so they must be copied first."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    _require_user_xattrs(config)
    os.setxattr(config, 'user.kioku_review', b'must-survive')

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert os.getxattr(config, 'user.kioku_review') == b'must-survive'
    assert 'KIOKU_MESH_AGENT_FAMILY' in config.read_text()


def test_repair_claude_code_preserves_a_posix_acl(tmp_path: Path) -> None:
    """An ACL is an xattr; losing it silently changes who may read a config holding identity env.

    Byte-compared rather than parsed: the named-user entry, the mask and the
    ordering all have to come through, and the raw value is what carries them.
    """
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    _require_user_xattrs(config)
    acl = _sample_posix_acl(tmp_path)

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert os.getxattr(config, 'system.posix_acl_access') == acl


def test_repair_claude_code_fails_closed_when_an_xattr_cannot_be_carried_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Unable to preserve is not permission to drop: nothing may be written."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    _require_user_xattrs(config)
    os.setxattr(config, 'user.kioku_review', b'must-survive')
    before = config.read_text()
    monkeypatch.setattr(mcp_install.os, 'setxattr', _raise_oserror(errno.EPERM, 'simulated setxattr denial'))

    with pytest.raises(RuntimeError, match='cannot be carried'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before
    assert os.getxattr(config, 'user.kioku_review') == b'must-survive'
    assert not list(tmp_path.glob('.claude.json.bak-*'))
    assert not list(tmp_path.glob('.claude.json.*.tmp'))


def test_repair_claude_code_accepts_an_unsettable_xattr_the_staged_file_already_matches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The SELinux case: a kernel-assigned label is unsettable but identical, so nothing is lost.

    Without this escape hatch --repair would fail closed on every SELinux host,
    where ``security.selinux`` is on each file and unprivileged ``setxattr``
    of it is denied.
    """
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    _require_user_xattrs(config)
    os.setxattr(config, 'user.kioku_review', b'inherited-by-the-kernel')
    real_setxattr = mcp_install.os.setxattr

    def deny_but_the_value_is_already_there(target: object, name: str, value: bytes) -> None:
        real_setxattr(target, name, value)  # stand in for the kernel having set it
        raise OSError(errno.EPERM, 'simulated privileged-namespace denial')

    monkeypatch.setattr(mcp_install.os, 'setxattr', deny_but_the_value_is_already_there)

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert os.getxattr(config, 'user.kioku_review') == b'inherited-by-the-kernel'
    assert 'KIOKU_MESH_AGENT_FAMILY' in config.read_text()


def test_repair_claude_code_fails_closed_when_an_xattr_appears_while_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The metadata half of the compare-and-swap (Codex re-review 4 on #287).

    Copying the xattrs takes a snapshot of them; another writer adding one after
    that snapshot means ``os.replace`` would carry the *stale* set onto the live
    file and drop the addition, with --repair reporting success.
    """
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    _require_user_xattrs(config)
    os.setxattr(config, 'user.before', b'already-there')
    before = config.read_text()
    real_copy_xattrs = mcp_install._copy_xattrs

    def copy_then_let_another_writer_add_one(source: Path, target_fd: int, *, destination: Path) -> None:
        real_copy_xattrs(source, target_fd, destination=destination)
        os.setxattr(source, 'user.added_during_staging', b'must-not-be-lost')

    monkeypatch.setattr(mcp_install, '_copy_xattrs', copy_then_let_another_writer_add_one)

    with pytest.raises(RuntimeError, match='its metadata changed on disk'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before
    assert os.getxattr(config, 'user.before') == b'already-there'
    assert os.getxattr(config, 'user.added_during_staging') == b'must-not-be-lost'
    assert not list(tmp_path.glob('.claude.json.bak-*'))
    assert not list(tmp_path.glob('.claude.json.*.tmp'))


def test_repair_claude_code_fails_closed_when_the_mode_changes_while_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The staged file wears the mode read at the start, so a chmod mid-repair would be undone too."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    config.chmod(0o600)
    before = config.read_text()
    real_copy_xattrs = mcp_install._copy_xattrs

    def copy_then_let_another_writer_widen_it(source: Path, target_fd: int, *, destination: Path) -> None:
        real_copy_xattrs(source, target_fd, destination=destination)
        source.chmod(0o640)

    monkeypatch.setattr(mcp_install, '_copy_xattrs', copy_then_let_another_writer_widen_it)

    with pytest.raises(RuntimeError, match='its metadata changed on disk'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert not list(tmp_path.glob('.claude.json.bak-*'))
    assert not list(tmp_path.glob('.claude.json.*.tmp'))


def test_repair_claude_code_keeps_a_mode_change_that_lands_before_the_metadata_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The staged file and the compare-and-swap baseline must come from one read (Codex re-review 5 on #287).

    Reading the mode and group with their own ``os.stat`` before taking the
    metadata snapshot left a window: a chmod landing in between is *inside* the
    snapshot, so the pre-replace re-check sees no change and lets the replace
    through, while the staged file still wears the mode read a syscall earlier.
    --repair then reports success and the concurrent 0640 is silently back to
    0600. Injecting the chmod immediately before the snapshot pins that window.
    """
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    config.chmod(0o600)
    real_snapshot = mcp_install._config_metadata_snapshot
    widened = False

    def widen_then_snapshot(path: Path, *, stage: str) -> tuple[object, ...]:
        nonlocal widened
        if not widened:
            widened = True
            path.chmod(0o640)
        return real_snapshot(path, stage=stage)

    monkeypatch.setattr(mcp_install, '_config_metadata_snapshot', widen_then_snapshot)

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert widened
    assert 'KIOKU_MESH_AGENT_FAMILY' in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o640
    assert not list(tmp_path.glob('.claude.json.*.tmp'))


def test_repair_claude_code_survives_a_filesystem_without_xattrs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No xattr support means there is nothing to carry over, not a reason to refuse."""
    config = tmp_path / '.claude.json'
    _user_scope_config(config)
    monkeypatch.setattr(
        mcp_install.os, 'listxattr', _raise_oserror(errno.ENOTSUP, 'simulated filesystem without xattrs')
    )

    repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert 'KIOKU_MESH_AGENT_FAMILY' in config.read_text()


def test_repair_claude_code_fails_closed_on_a_duplicate_json_key(tmp_path: Path) -> None:
    """Duplicate keys are legal JSON with reader-defined meaning; editing one is a guess."""
    config = tmp_path / '.claude.json'
    before = _minimal_document().replace('{\n  "mcpServers"', '{\n  "dup": 1,\n  "dup": 2,\n  "mcpServers"')
    config.write_text(before, encoding='utf-8')

    with pytest.raises(RuntimeError, match='duplicate JSON key'):
        repair_claude_code(config_path=config, project_dir=tmp_path / 'proj')

    assert config.read_text() == before


def test_claude_config_path_follows_claude_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    assert mcp_install._claude_config_path() == tmp_path / '.claude.json'

    monkeypatch.delenv('CLAUDE_CONFIG_DIR')
    monkeypatch.setattr(mcp_install.Path, 'home', classmethod(lambda _cls: tmp_path / 'home'))
    assert mcp_install._claude_config_path() == tmp_path / 'home' / '.claude.json'


def test_repair_claude_code_defaults_to_the_claude_config_dir_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No explicit path: the file `claude` itself writes is the one we repair."""
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    _user_scope_config(tmp_path / '.claude.json')
    monkeypatch.chdir(tmp_path)

    repair_claude_code()

    servers = json.loads((tmp_path / '.claude.json').read_text())['mcpServers']
    assert servers[DEFAULT_REGISTRY_NAME]['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'claude'}


# -- repair dispatch ----------------------------------------------------------------


def test_repair_dispatches_to_codex(tmp_path: Path) -> None:
    target = tmp_path / 'config.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\n\n[mcp_servers.kioku_mesh.env]\nMESH_MEM_AGENT_FAMILY = "codex"\n'
    )
    msg = repair(MCPClient.CODEX_CLI, config_path=target)
    assert 'repaired' in msg


def test_repair_dispatches_to_claude_code(tmp_path: Path) -> None:
    config = tmp_path / '.claude.json'
    _user_scope_config(config)

    msg = repair(MCPClient.CLAUDE_CODE, config_path=config, project_dir=tmp_path / 'proj')

    assert 'repaired' in msg
    servers = json.loads(config.read_text())['mcpServers']
    assert servers[DEFAULT_REGISTRY_NAME]['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'claude'}


# -- CLI wiring -----------------------------------------------------------------


def test_cli_mcp_install_codex_writes_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda name: f'/usr/bin/{name}')
    target = tmp_path / 'codex.toml'
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli'])
    assert rc == 0
    assert '[mcp_servers.kioku_mesh]' in target.read_text()
    assert 'wrote mcp_servers.kioku_mesh' in capsys.readouterr().out


def test_cli_mcp_install_codex_already_registered_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda name: f'/usr/bin/{name}')
    target = tmp_path / 'codex.toml'
    target.write_text('[mcp_servers.kioku_mesh]\ncommand = "/old"\n')
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli'])
    assert rc == 1
    assert '--force' in capsys.readouterr().err


def test_cli_mcp_install_missing_mesh_mem_mcp_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda _name: None)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli'])
    assert rc == 2
    assert 'kioku-mesh-mcp' in capsys.readouterr().err


def test_cli_mcp_install_extra_env_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda name: f'/usr/bin/{name}')
    target = tmp_path / 'codex.toml'
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(
        [
            'mcp',
            'install',
            '--client',
            'codex-cli',
            '-e',
            'ZENOH_CONNECT=tcp/192.168.1.5:7448',
        ]
    )
    assert rc == 0
    assert 'ZENOH_CONNECT = "tcp/192.168.1.5:7448"' in target.read_text()


def test_cli_mcp_install_rejects_malformed_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda name: f'/usr/bin/{name}')
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli', '-e', 'malformed'])
    assert rc == 2
    assert 'KEY=VALUE' in capsys.readouterr().err


def test_cli_mcp_install_repair_codex_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / 'codex.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\n\n[mcp_servers.kioku_mesh.env]\nMESH_MEM_AGENT_FAMILY = "codex"\n'
    )
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli', '--repair'])
    assert rc == 0
    assert 'KIOKU_MESH_AGENT_FAMILY = "codex"' in target.read_text()
    assert 'repaired' in capsys.readouterr().out


def test_cli_mcp_install_repair_codex_missing_entry_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / 'codex.toml'
    target.write_text('model = "gpt-5"\n')
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli', '--repair'])
    assert rc == 1
    assert 'kioku_mesh' in capsys.readouterr().err


def test_cli_mcp_install_repair_claude_code_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    _user_scope_config(tmp_path / '.claude.json')
    monkeypatch.chdir(tmp_path)

    rc = cli_main(['mcp', 'install', '--client', 'claude-code', '--repair'])

    assert rc == 0
    servers = json.loads((tmp_path / '.claude.json').read_text())['mcpServers']
    assert servers[DEFAULT_REGISTRY_NAME]['env'] == {'KIOKU_MESH_AGENT_FAMILY': 'claude'}
    assert 'repaired' in capsys.readouterr().out


def test_cli_mcp_install_repair_claude_code_missing_entry_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    (tmp_path / '.claude.json').write_text('{"mcpServers": {}}\n')
    monkeypatch.chdir(tmp_path)

    rc = cli_main(['mcp', 'install', '--client', 'claude-code', '--repair'])

    assert rc == 1
    assert DEFAULT_REGISTRY_NAME in capsys.readouterr().err


def test_cli_mcp_install_repair_claude_code_multiple_scopes_exits_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail-closed reaches the user as a non-zero exit with the scopes listed."""
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path))
    project = tmp_path / 'proj'
    project.mkdir()
    _write_claude_config(
        tmp_path / '.claude.json',
        {
            'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry()},
            'projects': {str(project): {'mcpServers': {DEFAULT_REGISTRY_NAME: _claude_entry()}}},
        },
    )
    monkeypatch.chdir(project)

    rc = cli_main(['mcp', 'install', '--client', 'claude-code', '--repair'])

    assert rc == 1
    err = capsys.readouterr().err
    assert 'registered in 2 scopes' in err
    assert 'user scope' in err and 'local scope' in err


def test_cli_mcp_install_repair_does_not_require_kioku_mesh_mcp_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """--repair edits an existing registration; it must not need `which('kioku-mesh-mcp')` to succeed."""
    monkeypatch.setattr(mcp_install.shutil, 'which', lambda _name: None)
    target = tmp_path / 'codex.toml'
    target.write_text(
        '[mcp_servers.kioku_mesh]\ncommand = "/x"\n\n[mcp_servers.kioku_mesh.env]\nMESH_MEM_CLIENT_ID = "codex-cli"\n'
    )
    monkeypatch.setattr(mcp_install, '_default_codex_config_path', lambda: target)
    rc = cli_main(['mcp', 'install', '--client', 'codex-cli', '--repair'])
    assert rc == 0
    assert 'KIOKU_MESH_CLIENT_ID = "codex-cli"' in target.read_text()
