"""Tests for the Modal walkthrough example that don't need Modal (or a registry).

The DAG in ``tasks.py`` is plain stardag — buildable locally with
zero-duration sleeps. The app module (selectors) only needs the ``modal``
package importable, not any Modal credentials.
"""

import importlib.util
import sys

import pytest

# The walkthrough uses APIs introduced together with reactive scheduling
# (StardagApp watchdog/limit-selector kwargs, build_trigger,
# RegistryConcurrencyLimiter). Skip the whole module when the installed
# stardag predates them (the examples lockfile pins the published
# release) — anything else (including bugs in the example modules) must
# surface, not skip. Module-existence sentinel: needs neither the modal
# extra nor an import (stardag.build the *attribute* is the build()
# function, so hasattr checks won't do).
if importlib.util.find_spec("stardag.build._registry_limiter") is None:
    pytest.skip(
        "installed stardag predates the walkthrough APIs (bump the "
        "examples lockfile after the release)",
        allow_module_level=True,
    )

from stardag.build import build_sequential
from stardag.testing import test_harness as _test_harness

from stardag_examples.modal.walkthrough.tasks import (
    MAX_SHARDS,
    MIN_SHARDS,
    LongScan,
    PlanShards,
    ProcessShard,
    ShardSpec,
    Summarize,
    report_dag,
)

# Explicit availability check instead of try/except ImportError, which
# would also swallow real import-time bugs in the example modules.
if importlib.util.find_spec("modal") is not None:
    from stardag_examples.modal.walkthrough import app as walkthrough_app
else:  # modal extra not installed
    walkthrough_app = None


def _fast_dag(source: str = "test-source"):
    return report_dag(source, shard_sleep_seconds=0.0, scan_sleep_seconds=0.0)


def test_dag_structure():
    root = _fast_dag()
    summary, scan = root.requires()
    assert isinstance(summary, Summarize)
    assert isinstance(scan, LongScan)
    assert isinstance(summary.requires(), PlanShards)
    # Same source everywhere -> deterministic task ids
    assert root.id == _fast_dag().id
    assert root.id != _fast_dag("other-source").id


def test_build_and_dynamic_fan_out():
    # Isolated target roots + no-op registry — no config or API access.
    with _test_harness():
        root = _fast_dag()
        build_sequential([root])
        assert root.complete()

        plan = PlanShards(source="test-source")
        specs = plan.load()
        assert MIN_SHARDS <= len(specs) <= MAX_SHARDS
        # Every dynamically yielded shard was built
        for spec in specs:
            shard = ProcessShard(source="test-source", spec=spec, sleep_seconds=0.0)
            assert shard.complete()
            assert shard.load()["value"] == float(spec.size)

        summary = root.summary.load()
        assert summary["num_shards"] == float(len(specs))
        assert summary["total"] == float(sum(spec.size for spec in specs))
        assert f"{len(specs)}" in root.load()


@pytest.mark.skipif(walkthrough_app is None, reason="modal is not installed")
def test_worker_and_limit_key_selectors():
    assert walkthrough_app is not None
    root = _fast_dag()
    summary, scan = root.requires()
    shard = ProcessShard(
        source="test-source", spec=ShardSpec(shard_id=0, size=1), sleep_seconds=0.0
    )

    assert walkthrough_app.worker_selector(scan) == "long"
    assert walkthrough_app.worker_selector(shard) == "default"
    assert walkthrough_app.worker_selector(root) == "default"

    assert walkthrough_app.limit_key_selector(shard) == [
        walkthrough_app.SHARD_LIMIT_KEY
    ]
    assert walkthrough_app.limit_key_selector(scan) == []
    assert walkthrough_app.limit_key_selector(summary) == []

    # The app enables the reactive-mode safety-net watchdog.
    assert walkthrough_app.app.watchdog_period_minutes == 5


def test_selectors_are_defined_in_an_importable_container_safe_module():
    """The selectors passed to StardagApp are captured by the serialized
    Modal functions (build/workers/tick), which deserialize in fresh
    containers by importing the callable's defining module. They must
    therefore live in an importable package module — NOT in the deploy
    script (which Modal loads as a loose top-level module named ``app``,
    unimportable in the container) — or a cold watchdog tick crashes with
    ``ModuleNotFoundError: No module named 'app'``. This test pins that
    property so a future refactor can't reintroduce the crash.
    """
    import importlib

    import cloudpickle

    from stardag_examples.modal.walkthrough import selectors

    for fn in (selectors.worker_selector, selectors.limit_key_selector):
        # Defined in the dedicated package module, not the deploy script.
        assert fn.__module__ == "stardag_examples.modal.walkthrough.selectors"
        # ...and that module is genuinely importable (what a container does).
        importlib.import_module(fn.__module__)

    # cloudpickle a closure capturing a selector (as the tick does), then
    # deserialize it with the selectors module absent from sys.modules —
    # forcing a real re-import by reference, the container's code path.
    def make_tick(selector):
        def tick(task):
            return selector(task)

        return tick

    blob = cloudpickle.dumps(make_tick(selectors.worker_selector))
    saved = sys.modules.pop("stardag_examples.modal.walkthrough.selectors", None)
    try:
        restored = cloudpickle.loads(blob)
    finally:
        if saved is not None:
            sys.modules["stardag_examples.modal.walkthrough.selectors"] = saved
    scan = LongScan(source="s", sleep_seconds=0.0)
    assert restored(scan) == "long"
