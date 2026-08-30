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
- ``STARDAG_MODAL_TEST_WORKSPACE``: if set, the workspace the *credentials
  actually belong to* must match. Prefer this wherever the credentials come
  from the environment rather than from a ``.modal.toml`` profile — CI, most
  obviously. ``STARDAG_MODAL_TEST_PROFILE`` compares a profile *name*, and a
  profile name is self-asserted: ``MODAL_PROFILE`` selects a section of a
  config file, but ``MODAL_TOKEN_ID`` / ``MODAL_TOKEN_SECRET`` take
  precedence over that file and are not bound to the name at all. So the
  profile check can pass while the token points somewhere else entirely. The
  workspace check resolves the workspace from the token itself, which is the
  thing worth asserting.

All live test modules should also carry ``pytestmark = pytest.mark.modal_live``
so the live tier can be excluded wholesale with ``pytest -m "not modal_live"``.
"""

from __future__ import annotations

import os
import typing

DEFAULT_TEST_VOLUME = "stardag-testing"

_TRUE = ("1", "true", "require")
_FALSE = ("0", "false", "skip")


_WORKSPACE_UNRESOLVED = object()
_workspace_cache: object | str | None = _WORKSPACE_UNRESOLVED


def _active_modal_workspace() -> str | None:
    """The workspace the configured Modal credentials belong to.

    Resolved from the token via Modal's own workspace lookup, deliberately
    *not* from ``STARDAG_MODAL_WORKSPACE`` — an env var a caller can set to
    any value is no use as a guard. Returns None when it cannot be
    determined (no credentials, no network, a running event loop, an API
    surface change), which the caller treats as a failed assertion rather
    than a pass.

    **Only a successful resolution is cached.** The guard runs once per
    live module — seven times over the tier — so caching the happy path is
    worth a round trip. Caching a *failure* would be actively wrong: a
    None is always read as a mismatch, so one transient error would fail
    every remaining module for the rest of the process, and re-running the
    lookup costs nothing that matters when the alternative is a wrong
    answer.
    """
    global _workspace_cache
    if _workspace_cache is not _WORKSPACE_UNRESOLVED:
        return typing.cast("str | None", _workspace_cache)

    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass  # no loop: `asyncio.run` below is available
    else:
        # Called from inside a running loop, where `asyncio.run` raises.
        # Not a resolution attempt, so nothing to cache.
        return None

    try:
        from stardag.integration.modal._metadata import _lookup_modal_workspace_aio

        workspace = asyncio.run(_lookup_modal_workspace_aio())
    except Exception:
        return None

    if workspace is not None:
        _workspace_cache = workspace
    return workspace


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

    # Before the credential check below, which *creates* a volume: verifying
    # the workspace only after having written to it would be backwards.
    expected_workspace = os.environ.get("STARDAG_MODAL_TEST_WORKSPACE")
    if expected_workspace:
        active_workspace = _active_modal_workspace()
        if active_workspace != expected_workspace:
            msg = (
                f"Modal credentials resolve to workspace "
                f"{active_workspace!r}, which does not match "
                f"STARDAG_MODAL_TEST_WORKSPACE={expected_workspace!r}"
            )
            if active_workspace is None:
                msg += " (no credentials, or the workspace lookup failed)"
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
