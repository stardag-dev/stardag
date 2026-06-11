import pytest
from stardag.build import build_sequential
from stardag.target.serialize import JSONSerializer, PandasDataFrameCSVSerializer

from stardag_examples.ml_pipeline.decorator_api import get_metrics_dag

try:
    import pandas as pd
except ImportError:
    pd = None


@pytest.mark.skipif(pd is None, reason="pandas is not installed")
def test_build_metrics_dag(default_in_memory_fs_target):
    # Fixed seed -> reproducible data, partition split, and metrics (regardless
    # of PYTHONHASHSEED), so the f1 assertion below is deterministic.
    metrics = get_metrics_dag(seed=0)
    assert isinstance(metrics._serializer, JSONSerializer)
    assert metrics.target().uri.endswith(".json")
    assert metrics.target().uri.startswith(
        "in-memory://examples/ml_pipeline/decorator_api/metrics/v0/"
    )
    assert isinstance(metrics.predictions._serializer, PandasDataFrameCSVSerializer)  # type: ignore
    assert metrics.predictions.target().uri.endswith(".csv")  # type: ignore
    build_sequential([metrics])
    assert metrics.complete()
    assert metrics.target().exists()
    metrics_dict = metrics.target().load()
    assert set(metrics_dict.keys()) == {"accuracy", "precision", "recall", "f1"}
    # f1 ≈ 0.64 with seed=0; assert a safe lower bound (better than random).
    assert metrics_dict["f1"] > 0.55
