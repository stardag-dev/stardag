"""Logging setup for stardag code running inside Modal containers.

Its own module because every entry point into a Modal container calls it —
the build function, the workers, the scheduler ticks, the bootstrap and the
watchdog — and those live in different modules of this package.
"""

from __future__ import annotations

import logging


def _setup_logging() -> None:
    """Setup logging for the modal app."""
    logging.basicConfig(level=logging.INFO)
