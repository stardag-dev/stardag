# `*DAG` Examples: ML Pipeline

A canonical, still realistic, example of an ML pipeline for a supervised machine learning problem.

The composable nature of `stardag` makes it easy to train and evaluate models on any data(sub)set and nest the standard "fit-predict-metrics" experiment flow into larger benchmarks, N-fold cross validation, hyperparameter search etc.

The file path of any persisted metric/result contains a hash of _all upstream dependencies that played a role in producing the asset_.

## Prerequisites

```shell
uv sync --extra ml-pipeline
```

NOTE: the submodules in this directory contains relative imports and should be run from the repo root directory using `uv run python -m stardag_examples.ml_pipeline.<module>`

## `.base.py`

The `base` module implements all business logic, independent of any dag/workflow frame work - just plain python without any persistence/caching or parallelism.

You can run the logic as is, by:

```shell
uv run python -m stardag_examples.ml_pipeline.base
```

To get the output (something like):

```
{
  "accuracy": 0.796,
  "precision": 0.819620253164557,
  "recall": 0.6379310344827587,
  "f1": 0.7174515235457064
}
```

## `.class_api.py`

Connects the pieces in the `.base` module into a `stardag` DAG using the "class API" (defining tasks by inheriting a `Task` base class)"

```shell
uv run python -m stardag_examples.ml_pipeline.class_api
```

with the output (something like)

```json
{
  "version": "0",
  "predictions": {
    "version": "0",
    "trained_model": {
      "version": "0",
      "model": {
        "type": "LogisticRegression",
        "penalty": "l2"
      },
      "dataset": {
        "version": "0",
        "dataset": {
          "__name": "Dataset",
          "__namespace": "examples.ml_pipeline.class_api",
          "version": "0",
          "dump": {
            "__name": "Dump",
            "__namespace": "examples.ml_pipeline.class_api",
            "version": "0",
            "date": "2024-09-29",
            "snapshot_slug": "default"
          },
          "params": {
            "category_thresholds": [
              0.0,
              0.5,
              1.0
            ]
          }
        },
        "filter": {
          "categories": null,
          "segments": null,
          "random_partition": {
            "num_buckets": 3,
            "include_buckets": [
              0,
              1
            ],
            "seed_salt": "default"
          }
        }
      },
      "seed": 0
    },
    "dataset": {
      "version": "0",
      "dataset": {
        "__name": "Dataset",
        "__namespace": "examples.ml_pipeline.class_api",
        "version": "0",
        "dump": {
          "__name": "Dump",
          "__namespace": "examples.ml_pipeline.class_api",
          "version": "0",
          "date": "2024-09-29",
          "snapshot_slug": "default"
        },
        "params": {
          "category_thresholds": [
            0.0,
            0.5,
            1.0
          ]
        }
      },
      "filter": {
        "categories": null,
        "segments": null,
        "random_partition": {
          "num_buckets": 3,
          "include_buckets": [
            2
          ],
          "seed_salt": "default"
        }
      }
    }
  }
}
{
  "accuracy": 0.7620396600566572,
  "precision": 0.8021978021978022,
  "recall": 0.5251798561151079,
  "f1": 0.6347826086956522
}
```

## `.decorator_api.py`

Connects the pieces in the `.base` module into a `stardag` DAG using the "class API" (defining tasks by inheriting a `Task` base class)"

```shell
uv run python -m stardag_examples.ml_pipeline.decorator_api
```

with the output very similar to above.
