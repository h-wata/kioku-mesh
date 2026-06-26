"""zenohd + zenoh-backend-rocksdb binary installer for kioku-mesh."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

from .core.paths import resolve_app_dir

_ZENOHD_REPO = 'eclipse-zenoh/zenoh'
_ROCKSDB_REPO = 'eclipse-zenoh/zenoh-backend-rocksdb'


def _detect_libc() -> str:
    """Return 'musl' or 'gnu' by probing ldd --version output."""
    try:
        result = subprocess.run(['ldd', '--version'], capture_output=True, text=True, check=False)
        if 'musl' in result.stdout + result.stderr:
            return 'musl'
    except (OSError, subprocess.SubprocessError):
        pass
    return 'gnu'


def _linux_target(arch: str, libc: str) -> str:
    mapping: dict[tuple[str, str], str] = {
        ('x86_64', 'gnu'): 'x86_64-unknown-linux-gnu',
        ('x86_64', 'musl'): 'x86_64-unknown-linux-musl',
        ('aarch64', 'gnu'): 'aarch64-unknown-linux-gnu',
        ('aarch64', 'musl'): 'aarch64-unknown-linux-musl',
        ('armv7', 'gnu'): 'armv7-unknown-linux-gnueabihf',
        ('armv7', 'musl'): 'armv7-unknown-linux-musleabihf',
        ('arm', 'gnu'): 'arm-unknown-linux-gnueabihf',
        ('arm', 'musl'): 'arm-unknown-linux-musleabihf',
    }
    key = (arch, libc)
    if key not in mapping:
        raise RuntimeError(f'unsupported Linux target: arch={arch!r} libc={libc!r}')
    return mapping[key]


def _rocksdb_lib_name() -> str:
    system = platform.system()
    if system == 'Darwin':
        return 'libzenoh_backend_rocksdb.dylib'
    if system == 'Windows':
        return 'zenoh_backend_rocksdb.dll'
    return 'libzenoh_backend_rocksdb.so'


def detect_target() -> str:
    """Return the upstream release target string for the current host.

    Maps (arch, OS, libc) to the Rust target triple used in zenoh release filenames.
    Raises RuntimeError for unsupported combinations.
    """
    machine = platform.machine().lower()
    system = platform.system()

    if machine in ('x86_64', 'amd64'):
        arch = 'x86_64'
    elif machine in ('aarch64', 'arm64'):
        arch = 'aarch64'
    elif machine.startswith('armv7'):
        arch = 'armv7'
    elif machine.startswith('arm'):
        arch = 'arm'
    else:
        raise RuntimeError(f'unsupported architecture: {machine!r}')

    if system == 'Linux':
        return _linux_target(arch, _detect_libc())
    if system == 'Darwin':
        return f'{arch}-apple-darwin'
    if system == 'Windows':
        if arch == 'x86_64':
            return 'x86_64-pc-windows-msvc'
        raise RuntimeError(f'unsupported arch on Windows: {arch!r}')
    raise RuntimeError(f'unsupported OS: {system!r}')


def release_urls(version: str, target: str) -> dict[str, str]:
    """Return download URLs and asset names for zenohd and zenoh-backend-rocksdb.

    All upstream releases use .zip. The '-standalone' variant is chosen for
    portability: it ships zenohd + plugin .so in a flat zip and works on any
    glibc-based distro. musl/darwin/windows have -standalone only.
    """
    zenohd_base = f'https://github.com/eclipse-zenoh/zenoh/releases/download/{version}'
    rocksdb_base = f'https://github.com/eclipse-zenoh/zenoh-backend-rocksdb/releases/download/{version}'
    # Use '-standalone' variant for portability.
    # linux-gnu also offers '-debian' (Debian-package layout), but '-standalone'
    # ships zenohd + plugin .so in a flat zip and works on any glibc-based distro.
    # musl/darwin/windows have -standalone only.
    zenohd_asset = f'zenoh-{version}-{target}-standalone.zip'
    rocksdb_asset = f'zenoh-backend-rocksdb-{version}-{target}-standalone.zip'
    return {
        'zenohd': f'{zenohd_base}/{zenohd_asset}',
        'zenohd_asset': zenohd_asset,
        'zenoh_backend_rocksdb': f'{rocksdb_base}/{rocksdb_asset}',
        'zenoh_backend_rocksdb_asset': rocksdb_asset,
    }


def _fetch_asset_digest(repo: str, tag: str, asset_name: str) -> str:
    """Fetch the SHA-256 digest for asset_name from GitHub Releases API.

    Returns a digest string like 'sha256:<hex>'.
    Raises ValueError if the asset is not found or the format is unexpected.
    """
    api_url = f'https://api.github.com/repos/{repo}/releases/tags/{tag}'
    req = urllib.request.Request(api_url, headers={'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        data = json.load(resp)
    for asset in data.get('assets', []):
        if asset['name'] == asset_name:
            digest = asset.get('digest', '')
            if not digest.startswith('sha256:'):
                raise ValueError(f'Unexpected digest format: {digest!r}')
            return digest
    raise ValueError(f'Asset {asset_name!r} not found in release {tag!r} of {repo!r}')


def download_and_verify(url: str, dest: Path, repo: str, tag: str, asset_name: str) -> Path:
    """Download url to dest and verify its SHA-256 checksum via GitHub Releases API.

    Raises ValueError with expected/got detail on checksum mismatch.
    """
    digest_str = _fetch_asset_digest(repo, tag, asset_name)
    expected_hex = digest_str.removeprefix('sha256:')
    urllib.request.urlretrieve(url, dest)
    actual_hex = hashlib.sha256(dest.read_bytes()).hexdigest()
    if actual_hex != expected_hex:
        dest.unlink(missing_ok=True)
        raise ValueError(f'SHA-256 mismatch for {asset_name}: expected {expected_hex}, got {actual_hex}')
    return dest


def extract_binary(archive: Path, binary_name: str, dest_dir: Path) -> Path:
    """Extract binary_name from a .tar.gz or .zip archive into dest_dir.

    Sets the executable bit and returns the installed path.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / binary_name
    if archive.name.endswith('.tar.gz') or archive.name.endswith('.tgz'):
        with tarfile.open(archive, 'r:gz') as tf:
            for member in tf.getmembers():
                if Path(member.name).name == binary_name:
                    f = tf.extractfile(member)
                    if f is not None:
                        dest.write_bytes(f.read())
                    break
            else:
                raise FileNotFoundError(f'{binary_name!r} not found in {archive.name}')
    elif archive.suffix == '.zip':
        with zipfile.ZipFile(archive) as zf:
            for name in zf.namelist():
                if Path(name).name == binary_name:
                    dest.write_bytes(zf.read(name))
                    break
            else:
                raise FileNotFoundError(f'{binary_name!r} not found in {archive.name}')
    else:
        raise ValueError(f'unsupported archive format: {archive.suffix!r}')
    dest.chmod(dest.stat().st_mode | 0o111)
    return dest


def default_bin_dir() -> Path:
    """Return the default install directory: ~/.local/share/kioku-mesh/bin."""
    return resolve_app_dir(Path.home() / '.local/share') / 'bin'


def install(version: str, bin_dir: Path, *, verbose: bool = False) -> dict[str, Path]:
    """Download and install zenohd + zenoh-backend-rocksdb into bin_dir.

    Returns a dict mapping 'zenohd' and 'zenoh_backend_rocksdb' to their installed paths.
    """
    target = detect_target()
    if verbose:
        print(f'target: {target}')
    urls = release_urls(version, target)
    bin_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zenohd_archive = tmp_path / 'zenohd.zip'
        if verbose:
            print(f'downloading {urls["zenohd"]}')
        download_and_verify(urls['zenohd'], zenohd_archive, _ZENOHD_REPO, version, urls['zenohd_asset'])
        zenohd_bin = 'zenohd.exe' if 'windows' in target else 'zenohd'
        results['zenohd'] = extract_binary(zenohd_archive, zenohd_bin, bin_dir)

        rocksdb_archive = tmp_path / 'zenoh-backend-rocksdb.zip'
        if verbose:
            print(f'downloading {urls["zenoh_backend_rocksdb"]}')
        download_and_verify(
            urls['zenoh_backend_rocksdb'], rocksdb_archive, _ROCKSDB_REPO, version, urls['zenoh_backend_rocksdb_asset']
        )
        results['zenoh_backend_rocksdb'] = extract_binary(rocksdb_archive, _rocksdb_lib_name(), bin_dir)
    return results
