"""The installed SDK version, resolved once.

Lives in its own leaf module rather than in ``stardag/__init__`` because
the registry client needs it *while* ``stardag/__init__`` is still
executing: the package root imports :mod:`stardag.registry` before it
would get around to defining ``__version__``, so a
``from stardag import __version__`` down there is an ImportError, not a
cycle you can shrug at. Everything that needs the version imports it from
here; ``stardag.__version__`` re-exports this value.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("stardag")
except PackageNotFoundError:
    # Package not installed (e.g., running from source in Modal container)
    __version__ = "0.0.0.dev"


__all__ = ["__version__"]
