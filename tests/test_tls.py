"""Tests for the mTLS provisioning core (`mesh_mem.tls`) and the `tls` CLI.

These never open a Zenoh session or touch the network: they exercise the pure
key/CSR/sign/install crypto and the CLI wiring against a sandboxed
``XDG_CONFIG_HOME``. The one cross-check that the *generated config* is actually
valid Zenoh JSON5 lives in test_cli_init-style rendering assertions here so a
wrong TLS key name fails the suite rather than only at zenohd startup.
"""

from __future__ import annotations

from pathlib import Path
import stat

from cryptography import x509
import pytest

from mesh_mem import tls
from mesh_mem.__main__ import _render_mesh_config
from mesh_mem.__main__ import _to_tls_endpoints
from mesh_mem.__main__ import main as cli_main


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect XDG_CONFIG_HOME so the TLS store lands in the sandbox."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    return tmp_path / 'xdg' / 'kioku-mesh' / 'tls'


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# -- pure crypto core ----------------------------------------------------------


def test_create_ca_writes_files_and_perms(xdg: Path) -> None:
    tls.create_ca()
    assert tls.ca_key_path().is_file()
    assert tls.ca_cert_path().is_file()
    assert _mode(tls.ca_key_path()) == 0o600
    info = tls.inspect_cert(tls.ca_cert_path().read_bytes())
    assert info.is_ca is True
    assert not info.expired


def test_request_keeps_key_private_and_csr_has_san(xdg: Path) -> None:
    tls.generate_key_and_csr(['192.168.3.10', 'hub.local'])
    assert _mode(tls.peer_key_path()) == 0o600
    csr = x509.load_pem_x509_csr(tls.peer_csr_path().read_bytes())
    san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    # IP SAN parsed as IPAddress, hostname as DNSName
    values = {str(g.value) for g in san}
    assert '192.168.3.10' in values
    assert 'hub.local' in values


def test_request_requires_a_san(xdg: Path) -> None:
    with pytest.raises(ValueError):
        tls.generate_key_and_csr([])


def test_sign_copies_san_and_sets_dual_eku(xdg: Path) -> None:
    tls.create_ca()
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    cert_pem = tls.sign_csr(csr_pem)
    cert = x509.load_pem_x509_certificate(cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert '10.0.0.5' in {str(g.value) for g in san}
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    oids = {e.dotted_string for e in eku}
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH.dotted_string in oids
    assert x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH.dotted_string in oids


def test_sign_rejects_csr_without_san(xdg: Path) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    tls.create_ca()
    key = ec.generate_private_key(ec.SECP256R1())
    bare_csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'x')]))
        .sign(key, hashes.SHA256())
    )
    with pytest.raises(ValueError, match='SubjectAlternativeName'):
        tls.sign_csr(bare_csr.public_bytes(__import__('cryptography').hazmat.primitives.serialization.Encoding.PEM))


def test_install_round_trip(xdg: Path) -> None:
    tls.create_ca()
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    cert_pem = tls.sign_csr(csr_pem)
    ca_pem = tls.ca_cert_path().read_bytes()
    tls.install(cert_pem, ca_pem)
    assert tls.peer_cert_path().is_file()
    assert tls.inspect_cert(tls.peer_cert_path().read_bytes()).sans == ['10.0.0.5']


def test_install_rejects_mismatched_ca(xdg: Path, tmp_path: Path) -> None:
    tls.create_ca()
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    cert_pem = tls.sign_csr(csr_pem)
    # A *different* CA cert must not validate this peer cert.
    other_ca_dir = tmp_path / 'other'
    other_ca_dir.mkdir()
    from datetime import datetime
    from datetime import timedelta
    from datetime import timezone

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    k = ec.generate_private_key(ec.SECP256R1())
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'rogue')])
    rogue = (
        x509.CertificateBuilder()
        .subject_name(subj)
        .issuer_name(subj)
        .public_key(k.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(k, hashes.SHA256())
    )
    rogue_pem = rogue.public_bytes(serialization.Encoding.PEM)
    with pytest.raises(ValueError, match='not issued by'):
        tls.install(cert_pem, rogue_pem)


def test_install_rejects_cert_for_a_different_peer_key(xdg: Path) -> None:
    # A cert that is validly CA-signed but minted from *another* peer's CSR must
    # not be installed next to this host's private key — the halves wouldn't match
    # and zenohd would only fail at handshake.
    tls.create_ca()
    # This host's own key + CSR.
    tls.generate_key_and_csr(['10.0.0.5'])
    ca_pem = tls.ca_cert_path().read_bytes()
    # A different peer requests + gets a cert from the same CA.
    _other_key, other_csr = tls.generate_key_and_csr(['10.0.0.6'])
    other_cert = tls.sign_csr(other_csr)
    # generate_key_and_csr overwrote peer.key with the second peer's key, so put
    # the first host's identity back: re-request to restore a key that does NOT
    # match other_cert.
    tls.generate_key_and_csr(['10.0.0.5'])
    with pytest.raises(ValueError, match='does not match the local private key'):
        tls.install(other_cert, ca_pem)


# -- copy-paste enrollment blobs ----------------------------------------------


def test_csr_blob_round_trips(xdg: Path) -> None:
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    blob = tls.encode_csr_blob(csr_pem)
    assert tls.CSR_LABEL in blob
    assert tls.decode_csr_blob(blob) == csr_pem


def test_decode_csr_blob_accepts_raw_pem(xdg: Path) -> None:
    # A bare .csr file (old scp flow) decodes through the same path.
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    assert tls.decode_csr_blob(csr_pem.decode()) == csr_pem


def test_decode_csr_blob_tolerates_surrounding_noise(xdg: Path) -> None:
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    blob = tls.encode_csr_blob(csr_pem)
    noisy = f'$ some prompt\nhere is the block:\n{blob}\n\n(end)\n'
    assert tls.decode_csr_blob(noisy) == csr_pem


def test_decode_csr_blob_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        tls.decode_csr_blob('not a blob at all')


def test_cert_bundle_round_trips(xdg: Path) -> None:
    tls.create_ca()
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    cert_pem = tls.sign_csr(csr_pem)
    ca_pem = tls.ca_cert_path().read_bytes()
    blob = tls.encode_cert_bundle(cert_pem, ca_pem)
    assert tls.BUNDLE_LABEL in blob
    assert tls.decode_cert_bundle(blob) == (cert_pem, ca_pem)


def test_decode_cert_bundle_rejects_wrong_label(xdg: Path) -> None:
    _key, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    csr_blob = tls.encode_csr_blob(csr_pem)
    # A CSR block pasted where a cert bundle was expected fails loudly.
    with pytest.raises(ValueError, match=tls.BUNDLE_LABEL):
        tls.decode_cert_bundle(csr_blob)


def test_blobs_never_contain_a_private_key(xdg: Path) -> None:
    # Belt-and-suspenders: neither blob may carry secret key material.
    tls.create_ca()
    key_pem, csr_pem = tls.generate_key_and_csr(['10.0.0.5'])
    cert_pem = tls.sign_csr(csr_pem)
    ca_pem = tls.ca_cert_path().read_bytes()
    secret = key_pem  # PKCS8 PEM of the peer's private key
    import base64

    for blob in (tls.encode_csr_blob(csr_pem), tls.encode_cert_bundle(cert_pem, ca_pem)):
        decoded = base64.b64decode(''.join(blob.splitlines()[1:-1]))
        assert b'PRIVATE KEY' not in decoded
        assert secret not in decoded


# -- endpoint scheme + config rendering ---------------------------------------


def test_to_tls_endpoints_swaps_only_cross_host() -> None:
    assert _to_tls_endpoints(
        [
            'tcp/1.2.3.4:7447',
            'tcp/127.0.0.1:7447',  # loopback stays plaintext
            'tcp/[::1]:7447',  # IPv6 loopback stays plaintext
            'tcp/localhost:7447',  # named loopback stays plaintext
            'udp/5.6.7.8:7447',  # UDP left untouched
        ]
    ) == [
        'tls/1.2.3.4:7447',
        'tcp/127.0.0.1:7447',
        'tcp/[::1]:7447',
        'tcp/localhost:7447',
        'udp/5.6.7.8:7447',
    ]


def test_render_mesh_config_keeps_loopback_plaintext(xdg: Path) -> None:
    body = _render_mesh_config('hub', ['tcp/127.0.0.1:7447', 'tcp/192.168.3.10:7447'], [], tls=True)
    assert 'tcp/127.0.0.1:7447' in body  # local clients still reach the router
    assert 'tls/192.168.3.10:7447' in body  # cross-host link encrypted
    assert 'enable_mtls: true' in body


def test_render_mesh_config_tls_block(xdg: Path) -> None:
    body = _render_mesh_config('hub', ['tcp/192.168.3.10:7447'], [], tls=True)
    assert 'tls/192.168.3.10:7447' in body
    assert 'enable_mtls: true' in body
    assert 'verify_name_on_connect: true' in body
    assert str(tls.ca_cert_path()) in body
    assert str(tls.peer_key_path()) in body


def test_render_mesh_config_plaintext_has_no_tls_block() -> None:
    body = _render_mesh_config('hub', ['tcp/192.168.3.10:7447'], [], tls=False)
    assert 'enable_mtls' not in body
    assert 'tcp/192.168.3.10:7447' in body


# -- CLI wiring ----------------------------------------------------------------


def test_cli_full_cycle_file_mode(xdg: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # File-based path: sign to a bundle file, install from that file.
    assert cli_main(['tls', 'init-ca']) == 0
    assert cli_main(['tls', 'request', '--san', '192.168.3.10']) == 0
    bundle = tmp_path / 'bundle.txt'
    assert cli_main(['tls', 'sign', str(tls.peer_csr_path()), '-o', str(bundle)]) == 0
    assert cli_main(['tls', 'install', str(bundle)]) == 0
    capsys.readouterr()
    assert cli_main(['tls', 'info']) == 0
    info_out = capsys.readouterr().out
    assert '192.168.3.10' in info_out


def test_cli_full_cycle_copy_paste(
    xdg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The headline flow: stdout blob from `request` is pasted into `sign`, whose
    # stdout blob is pasted into `install` — no files shuttled, no scp.
    import io

    assert cli_main(['tls', 'init-ca']) == 0
    capsys.readouterr()
    assert cli_main(['tls', 'request', '--san', '192.168.3.10']) == 0
    csr_blob = capsys.readouterr().out
    assert tls.CSR_LABEL in csr_blob

    monkeypatch.setattr('sys.stdin', io.StringIO(csr_blob))
    assert cli_main(['tls', 'sign']) == 0
    bundle_blob = capsys.readouterr().out
    assert tls.BUNDLE_LABEL in bundle_blob

    monkeypatch.setattr('sys.stdin', io.StringIO(bundle_blob))
    assert cli_main(['tls', 'install']) == 0
    assert tls.peer_cert_path().is_file()
    assert tls.inspect_cert(tls.peer_cert_path().read_bytes()).sans == ['192.168.3.10']


def test_cli_install_still_accepts_separate_cert_and_ca_files(xdg: Path, tmp_path: Path) -> None:
    # Back-compat: the original two-file --cert/--ca install path still works.
    assert cli_main(['tls', 'init-ca']) == 0
    assert cli_main(['tls', 'request', '--san', '10.0.0.5']) == 0
    cert_pem = tls.sign_csr(tls.peer_csr_path().read_bytes())
    crt = tmp_path / 'peer.crt'
    crt.write_bytes(cert_pem)
    assert cli_main(['tls', 'install', '--cert', str(crt), '--ca', str(tls.ca_cert_path())]) == 0
    assert tls.peer_cert_path().is_file()


def test_cli_sign_rejects_non_csr_input(xdg: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli_main(['tls', 'init-ca'])
    junk = tmp_path / 'junk.txt'
    junk.write_text('this is not a CSR')
    assert cli_main(['tls', 'sign', str(junk)]) == 2
    assert 'neither a KIOKU-MESH CSR block' in capsys.readouterr().err


def test_cli_install_mismatched_cert_ca_flags(xdg: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli_main(['tls', 'init-ca'])
    cli_main(['tls', 'request', '--san', '10.0.0.5'])
    # --cert without --ca is rejected with a clear message.
    rc = cli_main(['tls', 'install', '--cert', str(tmp_path / 'x.crt')])
    assert rc == 2
    assert 'must be given together' in capsys.readouterr().err


def test_cli_enroll_over_ssh(xdg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    # Stand up a CA in the same store, then stub `ssh ... tls sign` to sign the
    # CSR locally and return a bundle on stdout — exercising request -> remote
    # sign -> install end to end without a real SSH hop.
    import subprocess as _sp

    cli_main(['tls', 'init-ca'])
    capsys.readouterr()

    captured: dict[str, object] = {}

    def fake_run(
        cmd: list[str],
        input: str | None = None,  # noqa: A002
        capture_output: bool = False,
        text: bool = False,
        timeout: int | None = None,
    ) -> _sp.CompletedProcess:
        captured['cmd'] = cmd
        csr_pem = tls.decode_csr_blob(input)
        cert_pem = tls.sign_csr(csr_pem)
        bundle = tls.encode_cert_bundle(cert_pem, tls.ca_cert_path().read_bytes())
        return _sp.CompletedProcess(cmd, 0, stdout=bundle, stderr='')

    monkeypatch.setattr('mesh_mem.__main__.subprocess.run', fake_run)
    rc = cli_main(['tls', 'enroll', 'user@hub', '--san', '10.0.0.5', '--ssh-port', '2222'])
    assert rc == 0
    assert tls.peer_cert_path().is_file()
    assert tls.inspect_cert(tls.peer_cert_path().read_bytes()).sans == ['10.0.0.5']
    # SSH command was assembled with the port and destination.
    assert captured['cmd'][:3] == ['ssh', '-p', '2222']
    assert 'user@hub' in captured['cmd']


def test_cli_enroll_surfaces_remote_failure(
    xdg: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as _sp

    cli_main(['tls', 'init-ca'])

    def fake_run(
        cmd: list[str],
        input: str | None = None,  # noqa: A002
        capture_output: bool = False,
        text: bool = False,
        timeout: int | None = None,
    ) -> _sp.CompletedProcess:
        return _sp.CompletedProcess(cmd, 127, stdout='', stderr='kioku-mesh: command not found\n')

    monkeypatch.setattr('mesh_mem.__main__.subprocess.run', fake_run)
    rc = cli_main(['tls', 'enroll', 'hub', '--san', '10.0.0.5'])
    assert rc == 2
    err = capsys.readouterr().err
    assert 'remote `tls sign` failed' in err
    assert 'command not found' in err


def test_cli_enroll_failure_leaves_existing_peer_intact(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A failed enroll on an already-enrolled peer must NOT overwrite its working
    # key/cert with a fresh, non-matching key. Provision a peer first, then make
    # enroll fail at the remote-sign step and assert the key + cert are untouched.
    import subprocess as _sp

    cli_main(['tls', 'init-ca'])
    cli_main(['tls', 'request', '--san', '10.0.0.5'])
    cert_pem = tls.sign_csr(tls.peer_csr_path().read_bytes())
    tls.install(cert_pem, tls.ca_cert_path().read_bytes())
    good_key = tls.peer_key_path().read_bytes()
    good_crt = tls.peer_cert_path().read_bytes()
    good_csr = tls.peer_csr_path().read_bytes()

    def fake_run(
        cmd: list[str],
        input: str | None = None,  # noqa: A002
        capture_output: bool = False,
        text: bool = False,
        timeout: int | None = None,
    ) -> _sp.CompletedProcess:
        return _sp.CompletedProcess(cmd, 255, stdout='', stderr='ssh: connect: Connection refused\n')

    monkeypatch.setattr('mesh_mem.__main__.subprocess.run', fake_run)
    rc = cli_main(['tls', 'enroll', 'hub', '--san', '10.0.0.99'])
    assert rc == 2
    # Nothing committed: the still-valid key/cert/CSR are byte-for-byte unchanged.
    assert tls.peer_key_path().read_bytes() == good_key
    assert tls.peer_cert_path().read_bytes() == good_crt
    assert tls.peer_csr_path().read_bytes() == good_csr
    # And the original cert still matches the on-disk key (would raise if clobbered).
    tls.install(good_crt, tls.ca_cert_path().read_bytes())


def test_cli_enroll_malformed_bundle_leaves_existing_peer_intact(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same invariant when the remote "succeeds" but returns garbage on stdout.
    import subprocess as _sp

    cli_main(['tls', 'init-ca'])
    cli_main(['tls', 'request', '--san', '10.0.0.5'])
    cert_pem = tls.sign_csr(tls.peer_csr_path().read_bytes())
    tls.install(cert_pem, tls.ca_cert_path().read_bytes())
    good_key = tls.peer_key_path().read_bytes()

    def fake_run(
        cmd: list[str],
        input: str | None = None,  # noqa: A002
        capture_output: bool = False,
        text: bool = False,
        timeout: int | None = None,
    ) -> _sp.CompletedProcess:
        return _sp.CompletedProcess(cmd, 0, stdout='not a bundle at all', stderr='')

    monkeypatch.setattr('mesh_mem.__main__.subprocess.run', fake_run)
    assert cli_main(['tls', 'enroll', 'hub', '--san', '10.0.0.99']) == 2
    assert tls.peer_key_path().read_bytes() == good_key


def test_cli_enroll_quotes_remote_mesh_with_spaces(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A --remote-mesh path with spaces must be passed as one shell-safe token.
    import subprocess as _sp

    cli_main(['tls', 'init-ca'])
    captured: dict[str, object] = {}

    def fake_run(
        cmd: list[str],
        input: str | None = None,  # noqa: A002
        capture_output: bool = False,
        text: bool = False,
        timeout: int | None = None,
    ) -> _sp.CompletedProcess:
        captured['cmd'] = cmd
        cert_pem = tls.sign_csr(tls.decode_csr_blob(input))
        bundle = tls.encode_cert_bundle(cert_pem, tls.ca_cert_path().read_bytes())
        return _sp.CompletedProcess(cmd, 0, stdout=bundle, stderr='')

    monkeypatch.setattr('mesh_mem.__main__.subprocess.run', fake_run)
    rc = cli_main(['tls', 'enroll', 'hub', '--san', '10.0.0.5', '--remote-mesh', '/opt/my tools/kioku-mesh'])
    assert rc == 0
    remote = captured['cmd'][-1]
    assert remote == "'/opt/my tools/kioku-mesh' tls sign --days 825"


def test_cli_init_ca_refuses_overwrite(xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(['tls', 'init-ca']) == 0
    assert cli_main(['tls', 'init-ca']) == 1
    assert 'already exists' in capsys.readouterr().err


def test_cli_install_requires_local_key(xdg: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # init-ca + sign without ever running `request` on this host: no peer.key.
    cli_main(['tls', 'init-ca'])
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'x')]))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('x')]), critical=False)
        .sign(key, hashes.SHA256())
    )
    csr_path = tmp_path / 'x.csr'
    csr_path.write_bytes(csr.public_bytes(serialization.Encoding.PEM))
    cli_main(['tls', 'sign', str(csr_path), '-o', str(tmp_path / 'x.crt')])
    rc = cli_main(['tls', 'install', '--cert', str(tmp_path / 'x.crt'), '--ca', str(tls.ca_cert_path())])
    assert rc == 2
    assert 'no private key' in capsys.readouterr().err


def test_cli_init_tls_without_certs_errors(xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(['init', '--mode', 'hub', '--tls', '--listen', '192.168.3.10', '--print'])
    assert rc == 2
    assert 'certificates that are not present' in capsys.readouterr().err


def test_cli_init_tls_rejects_localhost(xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli_main(['init', '--mode', 'localhost', '--tls', '--print'])
    assert rc == 2
    assert 'hub / spoke' in capsys.readouterr().err


def test_cli_init_tls_rejects_cross_host_udp(xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Provision certs so the cert-missing guard doesn't fire first.
    cli_main(['tls', 'init-ca'])
    cli_main(['tls', 'request', '--san', '192.168.3.10'])
    cli_main(['tls', 'sign', str(tls.peer_csr_path()), '-o', str(tls.peer_cert_path())])
    cli_main(['tls', 'install', '--cert', str(tls.peer_cert_path()), '--ca', str(tls.ca_cert_path())])
    rc = cli_main(['init', '--mode', 'hub', '--tls', '--listen', 'udp/0.0.0.0:7447', '--print'])
    assert rc == 2
    assert 'UDP' in capsys.readouterr().err
