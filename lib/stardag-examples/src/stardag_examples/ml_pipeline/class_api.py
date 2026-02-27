import abc
import datetime
import json
import logging
import tempfile
import typing
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import stardag as sd
from pydantic import Field
from stardag.artifact import Artifact, JSONArtifact, MarkdownArtifact
from stardag.build import GlobalLockConfig
from stardag.target import LoadedT

from stardag_examples.ml_pipeline import base

logger = logging.getLogger(__name__)


sd.namespace("examples.ml_pipeline.class_api", scope=__name__)


class ExamplesMLPipelineBase(sd.Task[LoadedT], abc.ABC, typing.Generic[LoadedT]):
    __version__ = "0"
    version: str = __version__

    sleep_seconds: Annotated[float, sd.StardagField(hash_exclude=True)] = 3.0

    def run(self) -> None:
        logger.info(f"Running task: {self.__class__.__name__}")
        import time

        time.sleep(self.sleep_seconds)  # Simulate some work being done
        self._run()
        logger.info(f"Completed task: {self.__class__.__name__}")

    @abc.abstractmethod
    def _run(self) -> None:
        pass


class Dump(ExamplesMLPipelineBase[pd.DataFrame]):
    date: datetime.date = Field(default_factory=base.utc_today)
    snapshot_slug: str = Field(
        default="default",
        description=(
            "The slug for the dump. If you want to create multiple dumps "
            "for the same date, this must be used to differentiate them"
        ),
    )

    @property
    def _relpath_extra(self) -> str:
        return f"{self.date.isoformat()}/{self.snapshot_slug}"

    def _run(self):
        if not self.date == base.utc_today():
            raise ValueError("Date must be today")

        data = base.generate_data()
        self._save(data)


class Dataset(ExamplesMLPipelineBase[pd.DataFrame]):
    dump: sd.TaskLoads[pd.DataFrame] = Field(default_factory=Dump)
    params: base.ProcessParams = base.ProcessParams()

    def requires(self):
        return self.dump

    def _run(self):
        print("Processing data...")
        data = self.dump.load()
        processed_data = base.process_data(data, params=self.params)
        self._save(processed_data)


class Subset(ExamplesMLPipelineBase[pd.DataFrame]):
    dataset: sd.TaskLoads[pd.DataFrame]
    filter: base.DatasetFilter

    def requires(self):  # type: ignore
        return self.dataset

    def _run(self):
        print("Sub setting data...")
        data = self.dataset.load()  # type: ignore
        subset = self.filter(data)
        self._save(subset)


class TrainedModel(ExamplesMLPipelineBase[base.SKLearnClassifierModel]):
    model: base.HyperParameters = base.LogisticRegressionHyperParameters()
    dataset: Subset
    seed: int = 0

    def requires(self):  # type: ignore
        return self.dataset

    # TODO directory target!

    def _run(self):
        print("Training model...")
        dataset = self.dataset.load()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            model_dir = tmp_path / "model_dir"
            model = base.SKLearnClassifierModel(hyper_parameters=self.model)
            model = base.train_model(
                model=model,
                dataset=dataset,
                context=base.ModelFitContext(
                    model_dir=model_dir,
                    seed=self.seed,
                ),
            )
        self._save(model)


class Predictions(ExamplesMLPipelineBase[pd.DataFrame]):
    trained_model: TrainedModel
    dataset: Subset

    def requires(self):
        return {
            "trained_model": self.trained_model,
            "dataset": self.dataset,
        }

    def _run(self):
        print("Predicting...")
        model = self.trained_model.load()
        dataset = self.dataset.load()
        predictions = base.predict_model(model=model, dataset=dataset)
        self._save(predictions)


class Metrics(ExamplesMLPipelineBase[dict[str, float]]):
    predictions: Predictions

    def requires(self):
        return {
            "predictions": self.predictions,
            "dataset": self.predictions.dataset,
        }

    def _run(self):
        print("Calculating metrics...")
        dataset = self.predictions.dataset.load()
        predictions = self.predictions.load()
        metrics = base.get_metrics(dataset, predictions)
        self._save(metrics)

    # TODO: artifact format differs from prefect_on_complete_artifacts below
    # (prefect uses markdown table, native uses JSON key-value)
    def artifacts(self) -> list[Artifact]:
        metrics = self.load()
        return [JSONArtifact(name="metrics", body=metrics)]

    def prefect_on_complete_artifacts(self):
        from prefect.artifacts import MarkdownArtifact
        from stardag.integration.prefect import format_key

        metrics = self.load()
        markdown = f"""# Metrics Summary

| Metric    | Value |
|-----------|-------|
| Accuracy  | {metrics["accuracy"]} |
| Precision | {metrics["precision"]} |
| Recall    | {metrics["recall"]} |
| F1        | {metrics["f1"]} |
"""

        return [
            MarkdownArtifact(  # type: ignore
                markdown=markdown,
                key=format_key(f"{sd.TaskRef.from_task(self).slug}-result"),
                description="Metrics",
            )
        ]


class Benchmark(ExamplesMLPipelineBase[list[dict[str, Any]]]):
    train_dataset: Subset
    test_dataset: Subset
    models: tuple[base.HyperParameters, ...]
    seed: int = 0

    def requires(self):  # type: ignore
        return [
            Metrics(
                predictions=Predictions(
                    trained_model=TrainedModel(
                        model=model,
                        dataset=self.train_dataset,
                        seed=self.seed,
                    ),
                    dataset=self.test_dataset,
                )
            )
            for model in self.models
        ]

    def _run(self):
        metrics_s = [task.load() for task in self.requires()]
        metrics_and_params_s = [
            {**metrics, **hyper_parameters.model_dump(mode="json")}
            for metrics, hyper_parameters in zip(metrics_s, self.models)
        ]
        self._save(metrics_and_params_s)

    # TODO: artifact format differs from prefect_on_complete_artifacts below
    # (prefect uses TableArtifact, native uses markdown table)
    def artifacts(self) -> list[Artifact]:
        rows = self.load()
        if not rows:
            return []
        markdown = "# Benchmark Results\n\n" + pd.DataFrame(rows).to_markdown(
            index=False
        )
        return [MarkdownArtifact(name="benchmark", body=markdown)]

    def prefect_on_complete_artifacts(self):
        from prefect.artifacts import TableArtifact
        from stardag.integration.prefect import format_key

        rows = self.load()

        return [
            TableArtifact(  # type: ignore
                table=rows,
                key=format_key(f"{sd.TaskRef.from_task(self).slug}-result"),
                description="Metrics by model parameters",
            )
        ]


def get_metrics_dag(
    snapshot_slug: str = "default",
    preprocess_params: base.ProcessParams = base.ProcessParams(),
):
    """Get the DAG for calculating metrics for a single model.

    Args:
        snapshot_slug: The slug for the dump to use. In this mock example, it is just
            a way to update task ids to force re-running the DAG.
        preprocess_params: The parameters to use for preprocessing the data. Can also
            be changed to force re-running the DAG (and has some actual impact on
            training).

    Returns:
        Metrics: The metrics task representing the DAG for calculating metrics.
    """
    dump = Dump(snapshot_slug=snapshot_slug)

    dataset = Dataset(dump=dump, params=preprocess_params)

    train_dataset = Subset(
        dataset=dataset,
        filter=base.DatasetFilter(
            random_partition=base.RandomPartition(
                num_buckets=3,
                include_buckets=(0, 1),
            )
        ),
    )
    test_dataset = Subset(
        dataset=dataset,
        filter=base.DatasetFilter(
            random_partition=base.RandomPartition(
                num_buckets=3,
                include_buckets=(2,),
            )
        ),
    )

    trained_model = TrainedModel(
        model=base.LogisticRegressionHyperParameters(),
        dataset=train_dataset,
        seed=0,
    )

    predictions = Predictions(trained_model=trained_model, dataset=test_dataset)

    metrics = Metrics(predictions=predictions)

    return metrics


def get_benchmark_dag(
    snapshot_slug: str = "default",
    preprocess_params: base.ProcessParams = base.ProcessParams(),
):
    """Get the DAG for benchmarking multiple models.

    Args:
        snapshot_slug: The slug for the dump to use. In this mock example, it is
            just a way to update task ids to force re-running the DAG.
        preprocess_params: The parameters to use for preprocessing the data. Can also
            be changed to force re-running the DAG (and has some actual impact on
            training).
    Returns:
        Benchmark: The benchmark task representing the DAG for benchmarking multiple
            models.
    """
    dump = Dump(snapshot_slug=snapshot_slug)

    dataset = Dataset(dump=dump, params=preprocess_params)

    train_dataset = Subset(
        dataset=dataset,
        filter=base.DatasetFilter(
            random_partition=base.RandomPartition(
                num_buckets=3,
                include_buckets=(0, 1),
            )
        ),
    )
    test_dataset = Subset(
        dataset=dataset,
        filter=base.DatasetFilter(
            random_partition=base.RandomPartition(
                num_buckets=3,
                include_buckets=(2,),
            )
        ),
    )

    benchmark = Benchmark(
        train_dataset=train_dataset,
        test_dataset=test_dataset,
        models=(
            base.LogisticRegressionHyperParameters(penalty="l2"),
            base.DecisionTreeHyperParameters(criterion="gini", max_depth=3),
            base.DecisionTreeHyperParameters(criterion="gini", max_depth=10),
            base.DecisionTreeHyperParameters(criterion="entropy", max_depth=3),
            base.DecisionTreeHyperParameters(criterion="entropy", max_depth=10),
        ),
        seed=0,
    )

    return benchmark


if __name__ == "__main__":
    metrics = get_metrics_dag()
    print(metrics.model_dump_json(indent=2))
    sd.build([metrics], global_lock_config=GlobalLockConfig(enabled=True))
    print(json.dumps(metrics.load(), indent=2))
