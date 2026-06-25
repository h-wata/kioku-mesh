"""Tests for `mesh-mem mcp install` (#85).

Both client paths are driven by injecting probes (``which``, subprocess
``run``, config path) so the suite never reaches out to a real Claude Code
or Codex CLI install.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from kioku_mesh import mcp_install
from kioku_mesh.__main__ import main as cli_main
from kioku_mesh.mcp_install import _build_claude_add_command
from kioku_mesh.mcp_install import _render_codex_toml_block
from kioku_mesh.mcp_install import _replace_codex_block
from kioku_mesh.mcp_install import build_install_plan
from kioku_mesh.mcp_install import DEFAULT_REGISTRY_NAME
from kioku_mesh.mcp_install import install_claude_code
from kioku_mesh.mcp_install import install_codex_cli
from kioku_mesh.mcp_install import InstallPlan
from kioku_mesh.mcp_install import MCPClient
from kioku_mesh.mcp_install import parse_env_pairs

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
