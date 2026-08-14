"""Logging setup for stardag code running inside Modal containers.

Its own module because every entry point into a Modal container calls it —
the build function, the workers, the scheduler ticks, the bootstrap and the
watchdog — and those live in different modules of this package.

Always runs *after* the app's own ``container_setup`` hook (see
:mod:`._container_setup`), which is what lets an app own logging in these
containers: ``basicConfig`` returns early once the root logger has
handlers, so a hook that configured root logging wins and an app that did
not still gets this default.
"""

from __future__ import annotations

import logging


def _setup_logging() -> None:
    """Setup logging for the modal app."""
    logging.basicConfig(level=logging.INFO)
