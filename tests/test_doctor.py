"""Unit tests for :mod:`mesh_mem.doctor` and the ``kioku-mesh doctor`` CLI wiring.

Each check function is driven with monkeypatched probes / filesystem so the
suite stays deterministic on hosts that may or may not have zenohd installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kioku_mesh import doctor
from kioku_mesh.__main__ import main as cli_main
from kioku_mesh.doctor import _default_config_path
from kioku_mesh.doctor import _parse_zenoh_endpoint
from kioku_mesh.doctor import check_config_file
from kioku_mesh.doctor import check_state_dir_hardlinks
from kioku_mesh.doctor import check_tls_certs
from kioku_mesh.doctor import check_zenohd_binary
from kioku_mesh.doctor import check_zenohd_reachable
from kioku_mesh.doctor import CheckResult
from kioku_mesh.doctor import CheckStatus
from kioku_mesh.doctor import exit_code_for
from kioku_mesh.doctor import format_text
from kioku_mesh.doctor import to_json
from kioku_mesh.doctor import worst_status

# -- Severity / aggregation ----------------------------------------------------


def test_worst_status_empty_is_pass() -> None:
    assert worst_status([]) is CheckStatus.PASS


def test_worst_status_picks_highest_severity() -> None:
    results = [
        CheckResult(name='a', status=CheckStatus.PASS, summary='ok'),
        CheckResult(name='b', status=CheckStatus.WARN, summary='meh'),
        CheckResult(name='c', status=CheckStatus.FAIL, summary='bad'),
    ]
    assert worst_status(results) is CheckStatus.FAIL


def test_worst_status_warn_dominates_pass() -> None:
    results = [
        CheckResult(name='a', status=CheckStatus.PASS, summary='ok'),
        CheckResult(name='b', status=CheckStatus.WARN, summary='meh'),
    ]
    assert worst_status(results) is CheckStatus.WARN


@pytest.mark.parametrize(
    ('status', 'expected_code'),
    [
        (CheckStatus.PASS, 0),
        (CheckStatus.WARN, 1),
        (CheckStatus.FAIL, 2),
    ],
)
def test_exit_code_for(status: CheckStatus, expected_code: int) -> None:
    assert exit_code_for(status) == expected_code


# -- Endpoint parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('tcp/127.0.0.1:7447', ('127.0.0.1', 7447)),
        ('tcp/localhost:7447', ('localhost', 7447)),
        ('tcp/0.0.0.0:7448', ('0.0.0.0', 7448)),
        ('  tcp/127.0.0.1:7447  ', ('127.0.0.1', 7447)),
        ('tcp/[::1]:7447', ('[::1]', 7447)),  # bracketed IPv6 still parses
    ],
)
def test_parse_zenoh_endpoint_valid(raw: str, expected: tuple[str, int]) -> None:
    assert _parse_zenoh_endpoint(raw) == expected


@pytest.mark.parametrize(
    'raw',
    [
        '',
        'localhost:7447',  # missing scheme
        'udp/127.0.0.1:7447',  # UDP — not probed
        'tcp/127.0.0.1',  # missing port
        'tcp/127.0.0.1:notanint',
        'tcp/:7447',  # missing host
        'tcp/127.0.0.1:0',  # invalid port
        'tcp/127.0.0.1:99999',  # out of range
    ],
)
def test_parse_zenoh_endpoint_rejects_malformed(raw: str) -> None:
    assert _parse_zenoh_endpoint(raw) is None


# -- check_zenohd_reachable ----------------------------------------------------


def test_check_zenohd_reachable_pass() -> None:
    calls: list[tuple[tuple[str, int], float]] = []

    def fake_probe(addr: tuple[str, int], timeout: float) -> None:
        calls.append((addr, timeout))

    result = check_zenohd_reachable('tcp/127.0.0.1:7447', connect=fake_probe)
    assert result.status is CheckStatus.PASS
    assert calls == [(('127.0.0.1', 7447), doctor.ZENOH_CONNECT_TIMEOUT_SEC)]


def test_check_zenohd_reachable_connection_refused() -> None:
    def fake_probe(addr: tuple[str, int], timeout: float) -> None:
        raise ConnectionRefusedError(111, 'Connection refused')

    result = check_zenohd_reachable('tcp/127.0.0.1:7447', connect=fake_probe)
    assert result.status is CheckStatus.FAIL
    assert 'not reachable' in result.summary
    assert 'zenohd' in result.hint
    assert result.details['errno'] == 111


def test_check_zenohd_reachable_unparseable_endpoint() -> None:
    result = check_zenohd_reachable('udp/127.0.0.1:7447')
    assert result.status is CheckStatus.FAIL
    assert 'tcp/host:port' in result.hint
    assert result.details['endpoint'] == 'udp/127.0.0.1:7447'


def test_check_zenohd_reachable_reads_env_when_endpoint_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ZENOH_CONNECT', 'tcp/192.168.1.5:7448')

    def fake_probe(addr: tuple[str, int], timeout: float) -> None:
        pass

    result = check_zenohd_reachable(connect=fake_probe)
    assert result.status is CheckStatus.PASS
    assert result.details['host'] == '192.168.1.5'
    assert result.details['port'] == 7448


# -- check_zenohd_binary -------------------------------------------------------


def test_check_zenohd_binary_pass() -> None:
    result = check_zenohd_binary(which=lambda _name: '/usr/local/bin/zenohd')
    assert result.status is CheckStatus.PASS
    assert result.details['path'] == '/usr/local/bin/zenohd'


def test_check_zenohd_binary_missing() -> None:
    result = check_zenohd_binary(which=lambda _name: None)
    assert result.status is CheckStatus.FAIL
    assert 'not found on PATH' in result.summary
    assert 'cargo install' in result.hint or 'zenoh-backend-rocksdb' in result.hint
    assert result.details['path'] is None


# -- check_config_file ---------------------------------------------------------


def test_check_config_file_pass(tmp_path: Path) -> None:
    target = tmp_path / 'zenohd.json5'
    target.write_text('{}')
    result = check_config_file(target)
    assert result.status is CheckStatus.PASS
    assert str(target) in result.summary


def test_check_config_file_missing(tmp_path: Path) -> None:
    target = tmp_path / 'missing' / 'zenohd.json5'
    result = check_config_file(target)
    assert result.status is CheckStatus.FAIL
    assert 'kioku-mesh init' in result.hint


def test_default_config_path_honors_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`kioku-mesh init` writes here; doctor must look in the same place."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    assert _default_config_path() == tmp_path / 'xdg' / 'kioku-mesh' / 'zenohd.json5'


# -- check_state_dir_hardlinks -------------------------------------------------


def test_check_state_dir_hardlinks_pass_on_writable_fs(tmp_path: Path) -> None:
    """tmp_path on the test host's filesystem is expected to support hard links."""
    result = check_state_dir_hardlinks(tmp_path)
    assert result.status is CheckStatus.PASS
    # Probe files must be cleaned up.
    assert not any(p.name.startswith('.doctor.') for p in tmp_path.iterdir())


def test_check_state_dir_hardlinks_fail_without_hardlink_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate FAT / exFAT by making ``os.link`` raise EPERM."""

    def fake_link(_src: str, _dst: str) -> None:
        raise PermissionError(1, 'Operation not permitted')

    monkeypatch.setattr(doctor.os, 'link', fake_link)
    result = check_state_dir_hardlinks(tmp_path)
    assert result.status is CheckStatus.FAIL
    assert 'hard link' in result.summary
    assert 'ext4' in result.hint


def test_check_state_dir_hardlinks_fail_when_dir_not_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mkdir failure surfaces as FAIL, not as a hidden exception."""
    target = tmp_path / 'sub'

    def fake_mkdir(self: Path, *_args: object, **_kw: object) -> None:
        raise PermissionError(13, 'Permission denied')

    monkeypatch.setattr(Path, 'mkdir', fake_mkdir)
    result = check_state_dir_hardlinks(target)
    assert result.status is CheckStatus.FAIL
    assert 'not writable' in result.summary


# -- to_json / format_text -----------------------------------------------------


def _sample_results() -> list[CheckResult]:
    return [
        CheckResult(
            name='zenohd_binary',
            status=CheckStatus.PASS,
            summary='zenohd found at /usr/bin/zenohd',
            details={'path': '/usr/bin/zenohd'},
        ),
        CheckResult(
            name='config_file',
            status=CheckStatus.FAIL,
            summary='zenohd config missing',
            hint='Run kioku-mesh init',
            details={'path': '/x/y/zenohd.json5'},
        ),
    ]


def test_parse_zenoh_endpoint_accepts_tls() -> None:
    assert _parse_zenoh_endpoint('tls/192.168.3.10:7447') == ('192.168.3.10', 7447)


def test_check_tls_certs_not_configured_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No TLS in config, no peer cert -> PASS with a note (don't nag plaintext users).
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    result = check_tls_certs(config_path=tmp_path / 'absent.json5')
    assert result.status is CheckStatus.PASS
    assert result.details['tls_in_use'] is False


def test_check_tls_certs_plaintext_config_ignores_stale_certs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Reverted to a plaintext config but old cert files still linger: the active
    # config doesn't use TLS, so they're simply unused -> PASS, no FAIL/WARN.
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from datetime import timedelta

    from kioku_mesh import tls

    tls.create_ca()
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    tls.install(tls.sign_csr(csr_pem), tls.ca_cert_path().read_bytes())
    cfg = tmp_path / 'zenohd.json5'
    cfg.write_text('{ mode: "router" }', encoding='utf-8')  # no enable_mtls
    # Even an expired cert must not matter when the config is plaintext.
    real_now = tls._utcnow()
    monkeypatch.setattr(tls, '_utcnow', lambda: real_now + timedelta(days=10000))
    result = check_tls_certs(config_path=cfg)
    assert result.status is CheckStatus.PASS
    assert result.details['tls_in_use'] is False


def test_check_tls_certs_configured_but_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    cfg = tmp_path / 'zenohd.json5'
    cfg.write_text('{ transport: { link: { tls: { enable_mtls: true } } } }', encoding='utf-8')
    result = check_tls_certs(config_path=cfg)
    assert result.status is CheckStatus.FAIL
    assert result.details['missing']


def test_check_tls_certs_valid_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from kioku_mesh import tls

    tls.create_ca()
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    tls.install(tls.sign_csr(csr_pem), tls.ca_cert_path().read_bytes())
    cfg = tmp_path / 'zenohd.json5'
    cfg.write_text('enable_mtls: true', encoding='utf-8')
    result = check_tls_certs(config_path=cfg)
    assert result.status is CheckStatus.PASS
    assert result.details['days_remaining'] > 0


def test_check_tls_certs_expired_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    from datetime import timedelta

    from kioku_mesh import tls

    tls.create_ca()
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    tls.install(tls.sign_csr(csr_pem, days=825), tls.ca_cert_path().read_bytes())
    cfg = tmp_path / 'zenohd.json5'
    cfg.write_text('enable_mtls: true', encoding='utf-8')
    # Fast-forward "now" past the cert's validity window so it reads as expired.
    real_now = tls._utcnow()
    monkeypatch.setattr(tls, '_utcnow', lambda: real_now + timedelta(days=10000))
    result = check_tls_certs(config_path=cfg)
    assert result.status is CheckStatus.FAIL


def test_to_json_documented_shape() -> None:
    payload = json.loads(to_json(_sample_results()))
    assert payload['ok'] is False
    assert payload['worst_status'] == 'fail'
    assert {c['name'] for c in payload['checks']} == {'zenohd_binary', 'config_file'}
    assert payload['checks'][1]['status'] == 'fail'
    assert payload['checks'][1]['details']['path'] == '/x/y/zenohd.json5'
    assert 'version' in payload


def test_format_text_shows_status_and_hint() -> None:
    rendered = format_text(_sample_results())
    assert '[PASS] zenohd_binary' in rendered
    assert '[FAIL] config_file' in rendered
    assert 'hint: Run kioku-mesh init' in rendered
    assert 'verdict: one or more checks failed' in rendered


def test_format_text_all_pass_verdict() -> None:
    rendered = format_text(
        [
            CheckResult(name='a', status=CheckStatus.PASS, summary='ok'),
            CheckResult(name='b', status=CheckStatus.PASS, summary='ok'),
        ]
    )
    assert 'verdict: all checks passed' in rendered


# -- CLI wiring ----------------------------------------------------------------


def test_cli_doctor_text_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(doctor, 'run_all_checks', _sample_results)
    rc = cli_main(['doctor'])
    assert rc == 2  # FAIL severity from _sample_results
    out = capsys.readouterr().out
    assert '[PASS] zenohd_binary' in out
    assert 'verdict:' in out


def test_cli_doctor_json_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(doctor, 'run_all_checks', _sample_results)
    rc = cli_main(['doctor', '--json'])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['worst_status'] == 'fail'
    assert payload['ok'] is False


def test_cli_doctor_exit_zero_when_all_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def all_pass() -> list[CheckResult]:
        return [CheckResult(name='a', status=CheckStatus.PASS, summary='ok')]

    monkeypatch.setattr(doctor, 'run_all_checks', all_pass)
    rc = cli_main(['doctor'])
    assert rc == 0
    assert 'all checks passed' in capsys.readouterr().out


# -- check_shadow_visibility ---------------------------------------------------


def _make_index_with_shadowed(tmp_path: 'Path', *, n_shadowed: int = 0) -> object:
    """Create a LocalIndex with ``n_shadowed`` rebuild-shadowed observations."""
    from kioku_mesh.memory.local_index import LocalIndex  # noqa: PLC0415
    from kioku_mesh.models import Observation  # noqa: PLC0415

    idx = LocalIndex.connect(str(tmp_path / 'shadow_check.db'))
    for i in range(n_shadowed):
        obs = Observation(content=f'shadow obs {i}', project='p', agent_family='test')
        idx.upsert(obs)
        idx.mark_shadowed_missing(obs.observation_id, f'2026-06-0{i + 1}T00:00:00.000000Z')
    return idx


def test_check_shadow_visibility_pass(tmp_path: 'Path') -> None:
    """No shadowed rows → PASS."""
    from kioku_mesh.doctor import check_shadow_visibility  # noqa: PLC0415

    idx = _make_index_with_shadowed(tmp_path, n_shadowed=0)
    result = check_shadow_visibility(index=idx)
    assert result.status is CheckStatus.PASS
    assert 'shadowed observations: 0' in result.summary
    assert result.details['shadowed'] == 0


def test_check_shadow_visibility_warn(tmp_path: 'Path') -> None:
    """Shadowed rows → WARN with shadow explanation."""
    from kioku_mesh.doctor import check_shadow_visibility  # noqa: PLC0415

    idx = _make_index_with_shadowed(tmp_path, n_shadowed=2)
    result = check_shadow_visibility(index=idx)
    assert result.status is CheckStatus.WARN
    assert 'shadowed observations: 2' in result.summary
    assert 'hidden from search' in result.summary
    assert result.details['shadowed'] == 2
    assert result.hint != ''


def test_run_all_checks_includes_shadow() -> None:
    """run_all_checks must include a check named 'shadow_visibility'."""
    names = [r.name for r in doctor.run_all_checks()]
    assert 'shadow_visibility' in names


def test_run_all_checks_shadow_after_fts() -> None:
    """shadow_visibility must appear after 'fts5' in the check list."""
    names = [r.name for r in doctor.run_all_checks()]
    assert names.index('shadow_visibility') > names.index('fts5')


def test_check_shadow_visibility_json_output(tmp_path: 'Path') -> None:
    """to_json includes the shadow_visibility check entry."""
    from kioku_mesh.doctor import check_shadow_visibility  # noqa: PLC0415

    idx = _make_index_with_shadowed(tmp_path, n_shadowed=1)
    result = check_shadow_visibility(index=idx)
    payload = json.loads(to_json([result]))
    check_names = [c['name'] for c in payload['checks']]
    assert 'shadow_visibility' in check_names
    check = next(c for c in payload['checks'] if c['name'] == 'shadow_visibility')
    assert check['status'] == 'warn'
    assert check['details']['shadowed'] == 1


def test_check_shadow_visibility_text_output(tmp_path: 'Path') -> None:
    """format_text includes [WARN] shadow_visibility line."""
    from kioku_mesh.doctor import check_shadow_visibility  # noqa: PLC0415

    idx = _make_index_with_shadowed(tmp_path, n_shadowed=1)
    result = check_shadow_visibility(index=idx)
    rendered = format_text([result])
    assert '[WARN] shadow_visibility' in rendered
