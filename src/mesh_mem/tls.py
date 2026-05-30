"""mTLS certificate helpers backing `kioku-mesh tls` (CSR-based enrollment).

The mesh's transport-level trust is a small private PKI you run yourself:

  * one CA (``ca.key`` + ``ca.crt``) — the CA private key never leaves the host
    that created it, and is the only secret that must be guarded long-term;
  * one key pair per peer — the **private key is generated on the peer and never
    travels**. The peer emits a CSR (public information) that the CA signs.

This module is the pure, side-effect-light core: it generates keys/CSRs, signs
CSRs, and inspects certificates. The CLI layer in ``__main__`` wires these into
``tls init-ca`` / ``tls request`` / ``tls sign`` / ``tls install`` / ``tls info``
and decides what to print. Everything here is stdlib + ``cryptography`` only, so
it is unit-testable without a network or a zenohd binary.

Why CSR-based rather than "CA mints everything and scps it": keeping each peer's
private key on the peer that owns it means the only thing ever copied between
hosts is non-secret (the CSR going to the CA, the signed cert + CA cert coming
back). That is the same trust shape as ``ssh-copy-id`` pushing a public key.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import textwrap

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID
from cryptography.x509.oid import NameOID

from .paths import resolve_app_dir

# Default validity windows. The CA outlives peer certs by a wide margin so a
# routine peer-cert rotation never forces a CA rebuild (which would invalidate
# every peer at once). 825 days is the CABForum cap browsers enforce; we are not
# bound by it but it is a sane, well-trodden default for leaf certs.
DEFAULT_CA_DAYS = 3650
DEFAULT_CERT_DAYS = 825

# Elliptic-curve keys (P-256) over RSA: smaller files, faster handshakes, and
# fully supported by zenoh's rustls-based TLS stack. No tunable knob here on
# purpose — one good default keeps every peer's key interoperable.
_CURVE = ec.SECP256R1()


def tls_dir() -> Path:
    """Return ``~/.config/kioku-mesh/tls`` (XDG- and legacy-path aware).

    Mirrors how ``config`` / ``init`` resolve the config dir so the certificate
    store sits next to the generated ``zenohd.json5`` the mesh config points at.
    """
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return resolve_app_dir(Path(base)) / 'tls'


def ca_cert_path() -> Path:
    return tls_dir() / 'ca.crt'


def ca_key_path() -> Path:
    return tls_dir() / 'ca.key'


def peer_key_path() -> Path:
    return tls_dir() / 'peer.key'


def peer_csr_path() -> Path:
    return tls_dir() / 'peer.csr'


def peer_cert_path() -> Path:
    return tls_dir() / 'peer.crt'


# -- low-level IO --------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _write_secret(path: Path, data: bytes) -> None:
    """Write a private key with 0600 perms, created atomically-ish.

    The mode is set on open (not chmod after) so the key is never briefly
    world-readable between create and chmod.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    # Re-assert mode in case the file pre-existed with looser perms (O_CREAT
    # does not lower the mode of an existing file).
    os.chmod(path, 0o600)


def _write_public(path: Path, data: bytes) -> None:
    """Write a cert / CSR (non-secret) with ordinary 0644 perms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, 0o644)


def _build_san(values: list[str]) -> x509.SubjectAlternativeName:
    """Turn ``--san`` strings into a SubjectAlternativeName extension.

    Each value is classified as an IP address (``IPAddress`` general name) or a
    hostname (``DNSName``). ``verify_name_on_connect`` checks the endpoint a peer
    dials against these names, so a hub's cert must carry every address spokes
    use to reach it (e.g. its Tailscale IP and its LAN IP).
    """
    if not values:
        raise ValueError('at least one --san (the address peers dial this host on) is required')
    names: list[x509.GeneralName] = []
    for raw in values:
        v = raw.strip()
        if not v:
            continue
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(v)))
        except ValueError:
            names.append(x509.DNSName(v))
    if not names:
        raise ValueError('no usable SAN values supplied')
    return x509.SubjectAlternativeName(names)


# -- CA ------------------------------------------------------------------------


def create_ca(common_name: str = 'kioku-mesh-ca', days: int = DEFAULT_CA_DAYS) -> tuple[bytes, bytes]:
    """Create a self-signed CA, write ``ca.key`` (0600) + ``ca.crt``, return their PEMs.

    The CA can sign peer certs (``key_cert_sign``) but is explicitly not usable
    as a TLS endpoint itself (``basic_constraints`` CA:TRUE, no EKU).
    """
    key = ec.generate_private_key(_CURVE)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = _utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))  # tolerate small peer clock skew
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    _write_secret(ca_key_path(), key_pem)
    _write_public(ca_cert_path(), cert_pem)
    return key_pem, cert_pem


# -- peer key + CSR ------------------------------------------------------------


def generate_key_and_csr(sans: list[str], common_name: str | None = None) -> tuple[bytes, bytes]:
    """Generate this peer's private key + CSR. Writes ``peer.key`` (0600) + ``peer.csr``.

    The private key stays here; only the returned CSR PEM should be sent to the
    CA host. ``common_name`` defaults to the first SAN so the cert has a stable,
    human-recognizable subject.
    """
    san_ext = _build_san(sans)
    cn = common_name or sans[0]
    key = ec.generate_private_key(_CURVE)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .add_extension(san_ext, critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    _write_secret(peer_key_path(), key_pem)
    _write_public(peer_csr_path(), csr_pem)
    return key_pem, csr_pem


# -- signing -------------------------------------------------------------------


def sign_csr(csr_pem: bytes, days: int = DEFAULT_CERT_DAYS) -> bytes:
    """Sign a CSR with the on-disk CA and return the issued certificate PEM.

    The SAN is copied verbatim from the CSR — the requesting peer declares the
    addresses it will be reached on, and the CA vouches for them. The issued
    cert is valid for both ``serverAuth`` and ``clientAuth`` because every zenoh
    peer both listens (server) and dials (client) over the same identity.
    """
    csr = x509.load_pem_x509_csr(csr_pem)
    if not csr.is_signature_valid:
        raise ValueError('CSR signature is invalid (corrupt or tampered request)')
    ca_cert = x509.load_pem_x509_certificate(ca_cert_path().read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path().read_bytes(), password=None)

    try:
        san_ext = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as e:
        raise ValueError(
            'CSR has no SubjectAlternativeName; regenerate it with `kioku-mesh tls request --san ...`'
        ) from e

    now = _utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(san_ext, critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


# -- install -------------------------------------------------------------------


def install(cert_pem: bytes, ca_pem: bytes) -> None:
    """Place a signed peer cert + the CA cert into the local TLS store.

    Verifies the cert was issued by the supplied CA before writing, so a
    mismatched pair (wrong CA cert, stale peer cert) fails loudly here rather
    than as an opaque zenohd handshake error later.
    """
    cert = x509.load_pem_x509_certificate(cert_pem)
    ca_cert = x509.load_pem_x509_certificate(ca_pem)
    try:
        ca_cert.public_key().verify(
            cert.signature,
            cert.tbs_certificate_bytes,
            ec.ECDSA(cert.signature_hash_algorithm),  # type: ignore[arg-type]
        )
    except Exception as e:  # noqa: BLE001 - any verify failure means the pair does not match
        raise ValueError('peer certificate was not issued by the supplied CA certificate') from e
    # The cert must also match the private key that stays on this host. A cert
    # minted from another peer's CSR is still validly CA-signed, so the check
    # above would pass it — but zenohd would then load a cert/key pair whose
    # halves don't correspond and fail at handshake. Compare public keys so that
    # "wrong peer's cert" fails loudly here instead.
    key_path = peer_key_path()
    if key_path.is_file():
        local_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        spki = serialization.PublicFormat.SubjectPublicKeyInfo
        cert_pub = cert.public_key().public_bytes(serialization.Encoding.PEM, spki)
        key_pub = local_key.public_key().public_bytes(serialization.Encoding.PEM, spki)
        if cert_pub != key_pub:
            raise ValueError(
                f'signed certificate does not match the local private key at {key_path}; '
                'it appears to have been issued for a different peer'
            )
    _write_public(peer_cert_path(), cert_pem)
    _write_public(ca_cert_path(), ca_pem)


# -- inspection ----------------------------------------------------------------


@dataclass(frozen=True)
class CertInfo:
    subject: str
    issuer: str
    sans: list[str]
    not_valid_after: datetime
    is_ca: bool

    @property
    def days_remaining(self) -> int:
        return (self.not_valid_after - _utcnow()).days

    @property
    def expired(self) -> bool:
        return _utcnow() >= self.not_valid_after


def inspect_cert(cert_pem: bytes) -> CertInfo:
    """Summarize a certificate PEM for `tls info` / doctor."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    sans: list[str] = []
    try:
        san_ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        sans = [str(n.value) for n in san_ext]
    except x509.ExtensionNotFound:
        pass
    is_ca = False
    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        is_ca = bool(bc.ca)
    except x509.ExtensionNotFound:
        pass
    # not_valid_after_utc is timezone-aware (cryptography >= 42); fall back to
    # the naive attribute and stamp UTC for older builds.
    try:
        not_after = cert.not_valid_after_utc
    except AttributeError:
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
    return CertInfo(
        subject=cert.subject.rfc4514_string(),
        issuer=cert.issuer.rfc4514_string(),
        sans=sans,
        not_valid_after=not_after,
        is_ca=is_ca,
    )


# -- copy-paste enrollment blobs ----------------------------------------------
#
# scp'ing files between hosts (request -> scp -> sign -> scp -> install) is the
# part of enrollment that proved fiddly in practice: paths to track, an SSH path
# that has to exist. These helpers replace it with a single armored, copy-paste
# blob per hop — the same UX as pasting an auth code between terminals, working
# over any channel (Slack, a chat, a sticky note) with no added dependency.
#
# Only ever *non-secret* material is wrapped: a CSR (public) on the way to the
# CA, and the signed cert + CA cert (both public) on the way back. The two
# secrets — ``ca.key`` and each ``peer.key`` — are never encoded into a blob.

# Armor labels. The PEM-style ``-----BEGIN <label>-----`` framing gives the user
# one obviously-delimited block to select, and a label the decoder can demand so
# a CSR pasted where a bundle was expected fails with a clear message.
CSR_LABEL = 'KIOKU-MESH CSR'
BUNDLE_LABEL = 'KIOKU-MESH CERT BUNDLE'


def _armor(label: str, payload: bytes) -> str:
    """Wrap bytes in a base64, ``-----BEGIN <label>-----`` framed block.

    base64 (not the raw PEM) is what goes inside so the envelope's own
    ``-----BEGIN-----`` markers are the only ones present — a nested cert PEM
    can't be mistaken for the frame — and the whole thing is a clean, 64-column
    block that survives copy-paste and email/chat reflow.
    """
    b64 = base64.b64encode(payload).decode('ascii')
    body = '\n'.join(textwrap.wrap(b64, 64))
    return f'-----BEGIN {label}-----\n{body}\n-----END {label}-----'


def _dearmor(text: str, label: str) -> bytes:
    """Extract and base64-decode the payload of a ``-----BEGIN <label>-----`` block.

    Tolerant of surrounding noise (a shell prompt, a "paste this:" line, leading
    whitespace) so a sloppy copy still decodes. Raises ``ValueError`` with an
    actionable message when the block is missing or corrupt.
    """
    pattern = re.compile(
        r'-----BEGIN ' + re.escape(label) + r'-----(.*?)-----END ' + re.escape(label) + r'-----',
        re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        raise ValueError(
            f'no "{label}" block found in the pasted text — copy the whole block, '
            'including the -----BEGIN and -----END lines'
        )
    b64 = ''.join(match.group(1).split())
    try:
        return base64.b64decode(b64, validate=True)
    except Exception as e:  # noqa: BLE001 - any decode failure means a mangled paste
        raise ValueError(f'the "{label}" block is corrupt (a truncated or mangled paste)') from e


def encode_csr_blob(csr_pem: bytes) -> str:
    """Encode a CSR PEM as a copy-pasteable blob to hand to the CA host."""
    return _armor(CSR_LABEL, csr_pem)


def decode_csr_blob(text: str) -> bytes:
    """Return CSR PEM bytes from either an armored blob or a raw CSR PEM.

    ``tls sign`` feeds whatever it was given (a pasted blob, or a ``.csr`` file
    from the older scp flow) through here so both reach one signing path.
    """
    if CSR_LABEL in text:
        return _dearmor(text, CSR_LABEL)
    if 'BEGIN CERTIFICATE REQUEST' in text:
        return text.encode()
    raise ValueError(
        'input is neither a KIOKU-MESH CSR block nor a PEM certificate request; '
        'paste the block printed by `kioku-mesh tls request`, or pass a .csr file'
    )


def encode_cert_bundle(cert_pem: bytes, ca_pem: bytes) -> str:
    """Encode the signed peer cert + the CA cert as one copy-pasteable blob.

    Both are public. Bundling them means the peer pastes a single block and
    gets everything ``tls install`` needs — its own cert *and* the CA cert to
    trust — instead of shuttling two files back.
    """
    payload = json.dumps({'v': 1, 'cert': cert_pem.decode(), 'ca': ca_pem.decode()}).encode()
    return _armor(BUNDLE_LABEL, payload)


def decode_cert_bundle(text: str) -> tuple[bytes, bytes]:
    """Return ``(cert_pem, ca_pem)`` from an armored cert-bundle blob."""
    payload = _dearmor(text, BUNDLE_LABEL)
    try:
        obj = json.loads(payload)
        return obj['cert'].encode(), obj['ca'].encode()
    except (ValueError, KeyError, AttributeError, TypeError) as e:
        raise ValueError('the cert bundle decoded but is not the expected cert+ca structure') from e
