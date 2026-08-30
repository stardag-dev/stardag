"""Gating for the registry-live tier.

The failure this exists to prevent is specific and quiet. With no registry
configured the SDK falls back to a ``NoOpRegistry``: builds still run,
workers still execute, scenarios still pass -- and nothing whatsoever has
been checked about the registry, which is the entire subject of the tier.
A tier that can pass while testing nothing is worse than no tier, because
it is read as coverage.

So the guard asserts the two things a green run has to mean, and asserts
them before any scenario runs:

- the resolved registry is a real ``APIRegistry``, not any ``NoOp`` flavour;
- that registry answers an authenticated request over the network.

``require`` mode makes those failures rather than skips. It is the CI
default here, unlike ``STARDAG_MODAL_LIVE_TESTS``, whose ``auto`` default
exists so that a laptop without credentials is not a red build: this tier is
never reached by accident, since it lives outside the default ``testpaths``.

The Modal half of the gating is not reimplemented here.
``stardag.testing.modal.live_modal_guard`` already owns it -- credentials,
require mode, and the workspace assertion that checks the workspace the
*token* resolves to rather than a self-asserted profile name. This module
calls it.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable, NoReturn

if TYPE_CHECKING:
    from stardag.registry import APIRegistry

# Either fails or skips -- both raise, which is what lets the callers below
# treat a refusal as the end of the line rather than testing for it.
Refuse = Callable[[str], NoReturn]

ENV_ENABLED = "STARDAG_REGISTRY_LIVE_TESTS"
ENV_API_URL = "STARDAG_REGISTRY_LIVE_API_URL"

_TRUE = ("1", "true", "require")
_FALSE = ("0", "false", "skip")


def _mode() -> str:
    """``require`` | ``skip`` | ``auto`` -- what to do without a deployment.

    Unset means ``auto``: skip quietly when there is nothing deployed to
    talk to. CI sets ``1``, which turns every such skip into a failure.
    """
    raw = os.environ.get(ENV_ENABLED, "auto").strip().lower()
    if raw in _TRUE:
        return "require"
    if raw in _FALSE:
        return "skip"
    return "auto"


def registry_live_guard() -> str:
    """Assert a real, reachable registry is configured. Returns its URL.

    Called at module import in every scenario module, so a misconfiguration
    surfaces as a collection error rather than as a scenario that ran
    against nothing.
    """
    import pytest

    mode = _mode()
    if mode == "skip":
        pytest.skip(
            f"{ENV_ENABLED} is off",
            allow_module_level=True,
        )

    def refuse(message: str) -> NoReturn:
        if mode == "require":
            pytest.fail(message)
        pytest.skip(message, allow_module_level=True)

    api_url = os.environ.get(ENV_API_URL, "").strip()
    if not api_url:
        refuse(
            f"{ENV_API_URL} is not set: there is no deployed registry to test "
            "against. The session fixture deploys one; this tier cannot fall "
            "back to a local or in-process registry, because a registry a "
            "Modal container cannot reach is the one thing it does not cover."
        )

    registry = _assert_registry_is_real(refuse)
    _assert_registry_answers(registry, api_url, refuse)
    return api_url


def _assert_registry_is_real(refuse: Refuse) -> "APIRegistry":
    """The configured registry must be an ``APIRegistry``.

    Checked by *type* and not by behaviour, deliberately. A ``NoOpRegistry``
    accepts every call and returns plausible emptiness, so there is no
    request whose response distinguishes it from a registry that is merely
    idle. The type is the only honest test.
    """
    from stardag.registry import APIRegistry, NoOpRegistry, registry_provider

    registry = registry_provider.get()
    if isinstance(registry, NoOpRegistry):
        refuse(
            f"The configured registry is {type(registry).__name__}, a NoOp "
            "registry: it would accept every call in these scenarios and "
            "report nothing, and they would pass. This tier requires an "
            "APIRegistry pointed at a deployed API."
        )
    if not isinstance(registry, APIRegistry):
        refuse(
            f"The configured registry is {type(registry).__name__}, not an "
            "APIRegistry. These scenarios assert against the deployed API's "
            "own behaviour -- claim arbitration in a real transaction, wake "
            "candidates flagged on a real status write -- so a stand-in "
            "cannot stand in."
        )
    return registry


def _assert_registry_answers(
    registry: "APIRegistry", api_url: str, refuse: Refuse
) -> None:
    """One authenticated round-trip, so credentials are proven, not assumed.

    Made through the registry's *own* client, not a fresh httpx call: that
    is the code path every scenario will use, complete with its auth, its
    retry transport and its environment scoping, so this proves the thing
    that is about to be relied on rather than something adjacent.

    ``/health`` alone would not do -- it answers before any credential is
    looked at, so a revoked or absent API key sails past it and fails later
    inside a scenario, where it reads as a scheduling problem.
    """
    import httpx

    if not registry.environment_id:
        refuse(
            "The configured APIRegistry has no environment_id, so its "
            "requests are not scoped to the deployed environment and no "
            "meaningful round-trip can be made."
        )

    try:
        response = registry.client.get(
            f"{registry.api_url}/api/v1/builds",
            params={"environment_id": registry.environment_id, "limit": 1},
        )
    except httpx.HTTPError as error:
        refuse(f"The deployed registry at {api_url} is unreachable: {error!r}")

    if response.status_code != 200:
        refuse(
            f"An authenticated request to the deployed registry at {api_url} "
            f"returned {response.status_code}: {response.text[:200]!r}. The "
            "deployment is up, but this tier's credentials do not work "
            "against it."
        )
