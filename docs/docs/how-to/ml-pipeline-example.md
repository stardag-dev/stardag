# ML Pipeline Example

A complete example of building an ML pipeline with Stardag.

## Overview

This example demonstrates a canonical machine learning pipeline for supervised learning:

- Data loading and preprocessing
- Train/test splitting
- Model training
- Prediction and evaluation

The composable nature of Stardag makes it easy to:

- Train and evaluate models on any data subset
- Nest the standard "fit-predict-metrics" flow into larger benchmarks
- Run N-fold cross validation or hyperparameter search
- Track all upstream dependencies that produced each result

## Prerequisites

=== "uv"

    ```bash
    cd lib/stardag-examples
    uv sync --extra ml-pipeline
    ```

=== "pip"

    ```bash
    cd lib/stardag-examples
    pip install -e ".[ml-pipeline]"
    ```

## Project Structure

The example provides three implementations showing different API styles:

```
ml_pipeline/
├── base.py           # Plain Python logic (no Stardag)
├── class_api.py      # Class-based task definitions
└── decorator_api.py  # Decorator-based task definitions
```

## Running the Examples

### Plain Python (base.py)

The base module implements all business logic without any DAG framework - just plain Python:

=== "uv"

    ```bash
    uv run python -m stardag_examples.ml_pipeline.base
    ```

=== "pip"

    ```bash
    python -m stardag_examples.ml_pipeline.base
    ```

Output:

```json
{
  "accuracy": 0.796,
  "precision": 0.819,
  "recall": 0.637,
  "f1": 0.717
}
```

### Class API (class_api.py)

Wraps the base logic into Stardag tasks using class inheritance:

=== "uv"

    ```bash
    uv run python -m stardag_examples.ml_pipeline.class_api
    ```

=== "pip"

    ```bash
    python -m stardag_examples.ml_pipeline.class_api
    ```

This produces a full task specification showing the complete dependency graph, followed by metrics.

### Decorator API (decorator_api.py)

The same pipeline using the decorator syntax:

=== "uv"

    ```bash
    uv run python -m stardag_examples.ml_pipeline.decorator_api
    ```

=== "pip"

    ```bash
    python -m stardag_examples.ml_pipeline.decorator_api
    ```

## Key Concepts Demonstrated

### Deterministic Paths

The file path of any persisted result contains a hash of _all upstream dependencies_ that played a role in producing the asset. This means:

- Same parameters = same output location
- Change any upstream parameter = new output location
- Full reproducibility and cacheability

### Composability

Tasks can be composed into larger pipelines:

```python
# Single experiment
experiment = Metrics(
    predictions=Predictions(
        trained_model=TrainedModel(model=model, dataset=train_data),
        dataset=test_data,
    )
)

# Benchmark across multiple models
benchmark = [
    experiment_for_model(model)
    for model in [LogisticRegression(), DecisionTree(), RandomForest()]
]
```

### Data Filtering

The example includes flexible data filtering with:

- Category-based filtering
- Segment filtering
- Random partitioning for train/test splits

```python
train_filter = DataFilter(
    random_partition=RandomPartition(
        num_buckets=3,
        include_buckets=[0, 1],  # 2/3 for training
    )
)
```

## Example Output

When running with the class API, you'll see the full task specification:

```json
{
  "version": "0",
  "predictions": {
    "trained_model": {
      "model": { "type": "LogisticRegression", "penalty": "l2" },
      "dataset": {
        "filter": {
          "random_partition": {
            "num_buckets": 3,
            "include_buckets": [0, 1]
          }
        }
      }
    }
  }
}
```

Followed by the computed metrics:

```json
{
  "accuracy": 0.762,
  "precision": 0.802,
  "recall": 0.525,
  "f1": 0.634
}
```

## Next Steps

- [Integrate with Prefect](integrate-prefect.md) - Add observability to your ML pipeline
- [Integrate with Modal](integrate-modal.md) - Run training on serverless GPUs

## Source Code

View the full source on GitHub: [stardag-examples/ml_pipeline](https://github.com/stardag-dev/stardag/tree/main/lib/stardag-examples/src/stardag_examples/ml_pipeline)
