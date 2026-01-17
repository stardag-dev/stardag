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
    metrics = get_metrics_dag()
    assert isinstance(metrics._serializer, JSONSerializer)
    assert metrics.output().uri.endswith(".json")
    assert metrics.output().uri.startswith(
        "in-memory://examples/ml_pipeline/decorator_api/metrics/v0/"
    )
    assert isinstance(metrics.predictions._serializer, PandasDataFrameCSVSerializer)  # type: ignore
    assert metrics.predictions.output().uri.endswith(".csv")  # type: ignore
    build_sequential([metrics])
    assert metrics.complete()
    assert metrics.output().exists()
    metrics_dict = metrics.output().load()
    assert set(metrics_dict.keys()) == {"accuracy", "precision", "recall", "f1"}
    assert metrics_dict["f1"] > 0.50  # TODO fix seeds!
