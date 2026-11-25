"""Version resolution helpers for OpenCode Monitor."""

from importlib.metadata import PackageNotFoundError, version as distribution_version

from . import __version__ as FALLBACK_VERSION


_DISTRIBUTION_NAME = "ocmonitor"


def get_version() -> str:
    """Return installed package version with safe fallback.

    Resolution order:
    1. `ocmonitor` distribution metadata (PyPI package name)
    2. Local fallback version from `ocmonitor.__version__`
    """
    try:
        return distribution_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return FALLBACK_VERSION
