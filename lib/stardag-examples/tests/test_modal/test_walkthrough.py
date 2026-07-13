"""Tests for the Modal walkthrough example that don't need Modal (or a registry).

The DAG in ``tasks.py`` is plain stardag — buildable locally with
zero-duration sleeps. The app module (selectors) only needs the ``modal``
package importable, not any Modal credentials.
"""

import pytest
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

try:
    from stardag_examples.modal.walkthrough import app as walkthrough_app
except ImportError:  # modal extra not installed
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
