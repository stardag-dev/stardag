"""Tests for ResourceProvider, incl. cloudpickle survival.

Regression coverage for the Modal reactive-scheduling crash: a
``serialized=True`` Modal function (e.g. the scheduler ``tick`` /
``tick_watchdog``) captures ``registry_provider`` by value — the
``FunctionalResourceProvider`` class is created inside ``resource_provider``
and is not importable, so cloudpickle serializes the instance by value.
The unset sentinel is a bare ``object()`` whose identity does not survive
pickling, so a deserialized provider used to report itself as "already
set" to a stale sentinel and ``get()`` returned that bare ``object()``
(``AttributeError: 'object' object has no attribute ...`` in the
container). Providers must instead re-initialize their resource lazily in
the new process.
"""

import pytest

from stardag.registry import RegistryABC, registry_provider
from stardag.utils.resource_provider import resource_provider

# cloudpickle isn't a core/dev dependency (it comes with the `modal`
# extra); it's what Modal uses to serialize functions, and the bug under
# test is specifically about surviving that. Skip cleanly when it's absent
# rather than failing collection of the base suite.
cloudpickle = pytest.importorskip("cloudpickle")


def test_unset_provider_reinitializes_after_cloudpickle():
    """An *unset* provider (the common case at Modal deploy time: the
    provider hasn't been .get()'d yet) must re-initialize in the new
    process. This is the exact failure: the unset sentinel is a bare
    ``object()`` whose identity is lost through pickling, so without the
    fix ``get()`` returns that stale ``object()`` instead of running the
    factory."""
    provider = resource_provider(str, default_factory=lambda: "from-factory")
    assert not provider.is_initialized()

    copy = cloudpickle.loads(cloudpickle.dumps(provider))
    assert copy is not provider
    result = copy.get()
    assert result == "from-factory", f"expected re-init, got {result!r}"


def test_initialized_provider_does_not_carry_resource_across_cloudpickle():
    calls = {"n": 0}

    def factory() -> str:
        calls["n"] += 1
        return f"resource-{calls['n']}"

    provider = resource_provider(str, default_factory=factory)
    assert provider.get() == "resource-1"  # live resource in this process

    # The copy must re-run its factory in the new process, not carry the
    # original's live resource.
    copy = cloudpickle.loads(cloudpickle.dumps(provider))
    assert not copy.is_initialized()
    assert isinstance(copy.get(), str)


def test_registry_provider_resolves_after_cloudpickle():
    """The exact shape of the Modal watchdog crash: a cloudpickled
    ``registry_provider`` must resolve to a real registry, not a bare
    ``object()``."""
    copy = cloudpickle.loads(cloudpickle.dumps(registry_provider))
    resource = copy.get()
    assert isinstance(resource, RegistryABC)


def test_getstate_omits_live_resource():
    provider = resource_provider(str, default_factory=lambda: "x")
    provider.get()  # initialize
    assert "_resource" not in provider.__getstate__()


def test_set_default_factory_survives_cloudpickle():
    provider = resource_provider(str, default_factory=lambda: "default")
    provider.set_default_factory(lambda: "overridden")
    copy = cloudpickle.loads(cloudpickle.dumps(provider))
    assert copy.get() == "overridden"
