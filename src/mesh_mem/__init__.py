"""mesh-mem: Cross-agent distributed memory over a mesh transport (currently Zenoh)."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version

try:
    __version__ = version('mesh-mem')
except PackageNotFoundError:  # pragma: no cover
    __version__ = '0.0.0+unknown'

__all__ = ['__version__']
