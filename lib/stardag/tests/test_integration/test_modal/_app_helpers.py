"""Helpers shared by the ``test_stardag_app*.py`` modules: an image, a
``finalize()`` that captures the registered wrappers, a driver for sync and
async wrappers, and the two autouse fixtures every one of those modules
needs (import them by name to activate them)."""

from __future__ import annotations

import asyncio
import inspect
import typing
from unittest.mock import MagicMock, patch

import modal
import pytest

from stardag.integration.modal import StardagApp


def _make_image() -> modal.Image:
    return modal.Image.debian_slim()


# What ``@modal.concurrent`` was asked for, keyed by the callable it was
# applied to. Populated by the autouse stub below and read by
# ``TestInputConcurrency``; cleared per test.
_CONCURRENCY_REQUESTS: dict[typing.Callable, dict[str, int]] = {}


@pytest.fixture(autouse=True)
def _stub_modal_concurrent():
    """Replace ``modal.concurrent`` with a recording identity decorator.

    The real decorator returns an opaque ``PartialFunction`` whose raw
    callable is only reachable through Modal's synchronicity internals, and
    every test wants to *invoke* the registered wrapper. So the decorator is
    stubbed out: the wrappers stay plain functions, and what stardag asked
    Modal for is recorded instead. That request is the part worth pinning
    here anyway — how Modal implements input concurrency is Modal's
    business, and asserting it would pin their internals.
    """

    def concurrent(**kwargs):
        def decorator(fn):
            _CONCURRENCY_REQUESTS[fn] = kwargs
            return fn

        return decorator

    _CONCURRENCY_REQUESTS.clear()
    with patch("modal.concurrent", concurrent):
        yield
    _CONCURRENCY_REQUESTS.clear()


@pytest.fixture(autouse=True)
def _mock_secret_hydrate(monkeypatch):
    """StardagApp.finalize() validates a by-name api-key secret via
    Secret.hydrate(); stub it so tests neither hit the network nor depend
    on a secret actually existing. Tests exercising the missing-secret
    error override this explicitly."""
    monkeypatch.setattr(modal.Secret, "hydrate", lambda self, *a, **k: self)


def _invoke(fn, *args, **kwargs):
    """Call a registered wrapper, driving it to completion if it is async.

    The deployed ``tick`` is an ``async def`` so that concurrent inputs
    share one event loop and therefore one registry client; every other
    wrapper is sync. Tests call both through here rather than caring.
    """
    result = fn(*args, **kwargs)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


def _finalize_capturing_functions(app: StardagApp) -> dict:
    """finalize() ``app`` with ``modal.App.function`` stubbed out.

    Returns the registered callables by function name, so a test can
    invoke the deployed ``bootstrap`` / ``tick`` bodies in-process.
    """
    captured: dict = {}

    def capture_function(**kwargs):
        def decorator(fn):
            captured[kwargs.get("name", "unknown")] = fn
            return fn

        return decorator

    app.modal_app.function = capture_function  # type: ignore[assignment]
    with patch("stardag.integration.modal._app.get_target_roots_volumes") as mv:
        mv.return_value = MagicMock(by_volume_name={}, by_root_key={})
        app.finalize()
    return captured


_UNCOVERING_PATTERN = "stardag.registry.*"
"""A pattern that covers none of the task classes used in these tests.

Real and importable on purpose: ``finalize()`` expands the declared
patterns (that is how the deployed module list is frozen), so a fictional
package cannot be used by any test that goes through a deploy — which,
now that the coverage check runs from the *deployed* list, is all of them.
"""
