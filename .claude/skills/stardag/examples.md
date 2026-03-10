# Stardag SDK Examples & Patterns

## Minimal Example: Three API Levels

These three approaches are 100% equivalent:

### Decorator API

```python
import stardag as sd

@sd.task(name="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task(name="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)

root = get_sum(integers=get_range(limit=10))
sd.build(root)
print(root.load())  # 45
```

### Task Class API

```python
import stardag as sd

class Range(sd.Task[list[int]]):
    limit: int

    def run(self):
        self._save(list(range(self.limit)))

class Sum(sd.Task[int]):
    integers: sd.TaskLoads[list[int]]

    def requires(self):
        return self.integers

    def run(self):
        self._save(sum(self.integers.load()))

root = Sum(integers=Range(limit=10))
sd.build(root)
print(root.load())  # 45
```

### TargetTask API

```python
import stardag as sd
from stardag.target import LoadableSaveableFileSystemTarget
from stardag.target.serialize import FileSerializable, JSONSerializer

def default_relpath(task: sd.TargetTask) -> str:
    task_id = str(task.id)
    return "/".join([task.get_name(), task_id[:2], task_id[2:4], f"{task_id}.json"])

class Range(sd.TargetTask[LoadableSaveableFileSystemTarget[list[int]]]):
    limit: int

    def target(self) -> LoadableSaveableFileSystemTarget[list[int]]:
        return FileSerializable(
            wrapped=sd.get_file_target(default_relpath(self)),
            serializer=JSONSerializer(list[int]),
        )

    def run(self):
        self.target().save(list(range(self.limit)))

class Sum(sd.TargetTask[LoadableSaveableFileSystemTarget[int]]):
    integers: sd.SubClass[sd.TargetTask[LoadableSaveableFileSystemTarget[list[int]]]]

    def requires(self):
        return self.integers

    def target(self) -> LoadableSaveableFileSystemTarget[int]:
        return FileSerializable(
            wrapped=sd.get_file_target(default_relpath(self)),
            serializer=JSONSerializer(int),
        )

    def run(self):
        return self.target().save(sum(self.integers.target().load()))

root = Sum(integers=Range(limit=10))
sd.build(root)
```

## ML Pipeline Pattern (Class API)

Real-world pattern showing a full ML pipeline with shared base class, namespacing,
versioning, artifacts, and DAG composition.

```python
import abc
import datetime
import typing
from typing import Annotated, Any

import pandas as pd
import stardag as sd
from pydantic import Field
from stardag.artifact import Artifact, JSONArtifact, MarkdownArtifact
from stardag.build import GlobalLockConfig

sd.namespace("examples.ml_pipeline", scope=__name__)


# Shared base with versioning and sleep simulation
class PipelineBase(sd.Task[T], abc.ABC, typing.Generic[T]):
    __version__ = "1"
    version: str = __version__
    sleep_seconds: Annotated[float, sd.StardagField(hash_exclude=True)] = 0.0

    def run(self) -> None:
        self._run()

    @abc.abstractmethod
    def _run(self) -> None:
        pass


class IngestData(PipelineBase[pd.DataFrame]):
    date: datetime.date = Field(default_factory=datetime.date.today)
    source: str = "default"

    @property
    def _relpath_extra(self) -> str:
        return f"{self.date.isoformat()}/{self.source}"

    def _run(self):
        data = fetch_raw_data(self.source)
        self._save(data)


class PrepareDataset(PipelineBase[pd.DataFrame]):
    raw: sd.TaskLoads[pd.DataFrame] = Field(default_factory=IngestData)
    min_rows: int = 100

    def requires(self):
        return self.raw

    def _run(self):
        df = self.raw.load()
        cleaned = df.dropna().query(f"len(df) >= {self.min_rows}")
        self._save(cleaned)


class TrainModel(PipelineBase[dict]):
    dataset: PrepareDataset
    learning_rate: float = 0.01
    epochs: int = 100
    seed: int = 42

    def requires(self):
        return self.dataset

    def _run(self):
        data = self.dataset.load()
        model_params = train(data, lr=self.learning_rate, epochs=self.epochs)
        self._save(model_params)


class EvaluateModel(PipelineBase[dict[str, float]]):
    model: TrainModel
    test_data: PrepareDataset

    def requires(self):
        return {"model": self.model, "test_data": self.test_data}

    def _run(self):
        model = self.model.load()
        test = self.test_data.load()
        metrics = evaluate(model, test)
        self._save(metrics)

    def artifacts(self) -> list[Artifact]:
        m = self.load()
        return [
            JSONArtifact(name="metrics", body=m),
            MarkdownArtifact(
                name="summary",
                body=f"# Evaluation\n| Metric | Value |\n|--------|-------|\n"
                + "\n".join(f"| {k} | {v:.4f} |" for k, v in m.items()),
            ),
        ]


# Compose the DAG
def build_pipeline(source: str = "default"):
    raw = IngestData(source=source)
    dataset = PrepareDataset(raw=raw)
    model = TrainModel(dataset=dataset)
    metrics = EvaluateModel(model=model, test_data=dataset)
    return metrics


if __name__ == "__main__":
    root = build_pipeline()
    sd.build(root, global_lock_config=GlobalLockConfig(enabled=True))
    print(root.load())
```

## Common Patterns

### DAG Composition Factory

```python
def create_etl_dag(source: str, target_table: str):
    """Factory function that builds a parameterized DAG."""
    extract = ExtractTask(source=source)
    transform = TransformTask(raw_data=extract)
    load = LoadTask(data=transform, table=target_table)
    return load

# Run different configurations
sd.build([
    create_etl_dag("api", "api_data"),
    create_etl_dag("csv", "csv_data"),
])
```

### Benchmark / Fan-Out Pattern

```python
class Benchmark(sd.Task[list[dict]]):
    configs: tuple[Config, ...]
    dataset: PrepareDataset

    def requires(self):
        return [
            EvaluateModel(
                model=TrainModel(dataset=self.dataset, config=cfg),
                test_data=self.dataset,
            )
            for cfg in self.configs
        ]

    def run(self):
        results = [task.load() for task in self.requires()]
        self._save(results)
```

### Conditional Dependencies

```python
class ConditionalTask(sd.Task[dict]):
    use_cache: bool = True

    def requires(self):
        if self.use_cache:
            return CachedDataTask()
        return FreshDataTask()
```

### Default Factory Dependencies

```python
class DownstreamTask(sd.Task[int]):
    # Default upstream if not explicitly provided
    data: sd.TaskLoads[pd.DataFrame] = Field(default_factory=DefaultDataTask)

    def requires(self):
        return self.data
```

### Hash-Excluded Runtime Parameters

```python
class FlexibleTask(sd.Task[dict]):
    # These affect the task ID (data parameters)
    dataset_name: str
    model_type: str

    # These DON'T affect the task ID (runtime tuning)
    num_workers: Annotated[int, sd.StardagField(hash_exclude=True)] = 4
    verbose: Annotated[bool, sd.StardagField(hash_exclude=True)] = False
    timeout: Annotated[float, sd.StardagField(hash_exclude=True)] = 300.0
```

### Inspecting the DAG

```python
task = build_pipeline()

# Print task as JSON (shows full parameter tree)
print(task.model_dump_json(indent=2))

# Get task reference
ref = sd.TaskRef.from_task(task)
print(f"Name: {ref.name}, ID: {ref.id}")

# Flatten nested dependencies
all_tasks = sd.flatten_task_struct(task.requires())
for t in all_tasks:
    print(f"  {t.get_name()} ({t.id})")
```

## Source Code Reference

Full working examples are available in the repository:

- `stardag/lib/stardag-examples/src/stardag_examples/general/task_api_three_levels.py`
- `stardag/lib/stardag-examples/src/stardag_examples/ml_pipeline/class_api.py`
- `stardag/lib/stardag-examples/src/stardag_examples/ml_pipeline/decorator_api.py`
- `stardag/lib/stardag-examples/src/stardag_examples/general/artifacts_demo.py`

For more examples and tutorials, visit [docs.stardag.com/getting-started](https://docs.stardag.com/getting-started/).
