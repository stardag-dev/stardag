"""Gating for live-Modal tests.

Live tests hit a real Modal workspace: they deploy apps, create volumes and
dicts, and run containers. They are gated centrally by
:func:`live_modal_guard`, controlled via environment variables:

- ``STARDAG_MODAL_LIVE_TESTS``:
    - ``"auto"`` (default) — run if Modal credentials work, skip otherwise.
    - ``"1"`` / ``"true"`` — require: *fail* (instead of skip) when
      credentials are missing or the profile guard doesn't match. Use in CI
      jobs that are supposed to run the live tier, so misconfiguration
      doesn't silently skip it.
    - ``"0"`` / ``"false"`` — always skip.
- ``STARDAG_MODAL_TEST_PROFILE``: if set, the active Modal profile must
  match, otherwise the live tests are skipped (or fail in require mode).
  This guards against accidentally running live tests — which create
  apps/volumes visible to the whole workspace — against a shared or
  production-adjacent profile.

All live test modules should also carry ``pytestmark = pytest.mark.modal_live``
so the live tier can be excluded wholesale with ``pytest -m "not modal_live"``.
"""

from __future__ import annotations

import os

DEFAULT_TEST_VOLUME = "stardag-testing"

_TRUE = ("1", "true", "require")
_FALSE = ("0", "false", "skip")


def _active_modal_profile() -> str | None:
    """Best-effort lookup of the active Modal profile name.

    Uses a private ``modal.config`` attribute (no public API exposes the
    active profile); returns None if the import surface changes.
    """
    try:
        from modal.config import _profile  # pyright: ignore[reportPrivateUsage]

        return _profile
    except Exception:
        return None


def live_modal_guard(volume_name: str = DEFAULT_TEST_VOLUME) -> None:
    """Module-level guard for live-Modal test modules.

    Call at import time (after the ``import modal`` guard). Skips or fails
    the module according to ``STARDAG_MODAL_LIVE_TESTS`` and
    ``STARDAG_MODAL_TEST_PROFILE`` (see module docstring), and verifies
    working credentials by hydrating (creating if missing) the shared test
    volume.

    Args:
        volume_name: Volume used for the credential check. Created if
            missing, so live test environments need no manual volume setup.
    """
    import pytest  # deferred: stardag.testing must import without pytest installed

    mode = os.environ.get("STARDAG_MODAL_LIVE_TESTS", "auto").strip().lower()
    if mode in _FALSE:
        pytest.skip(
            "Live Modal tests disabled (STARDAG_MODAL_LIVE_TESTS=0)",
            allow_module_level=True,
        )
    required = mode in _TRUE

    expected_profile = os.environ.get("STARDAG_MODAL_TEST_PROFILE")
    if expected_profile:
        active = _active_modal_profile()
        if active != expected_profile:
            msg = (
                f"Active Modal profile {active!r} does not match "
                f"STARDAG_MODAL_TEST_PROFILE={expected_profile!r}"
            )
            if required:
                pytest.fail(msg)
            pytest.skip(msg, allow_module_level=True)

    import modal
    from modal.exception import AuthError

    try:
        modal.Volume.from_name(volume_name, create_if_missing=True).hydrate()
    except AuthError as exc:
        msg = f"Modal credentials not available: {exc}"
        if required:
            pytest.fail(msg)
        pytest.skip(msg, allow_module_level=True)
