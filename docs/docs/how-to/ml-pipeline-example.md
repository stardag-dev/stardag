# ML Pipeline Example

Example of a typical ML pipeline with Stardag.

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
- Track upstream dependencies that produced each result

## Prerequisites

Clone the repo

=== "HTTPS"

    Clone using the web URL.

    ```sh
    git clone https://github.com/stardag-dev/stardag.git
    cd stardag/lib/stardag-examples
    ```

=== "SSH"

    Use a password-protected SSH key.

    ```sh
    git clone git@github.com:stardag-dev/stardag.git
    cd stardag/lib/stardag-examples
    ```

=== "GitHub CLI"

    Use the GitHub official CLI. [Learn more](https://cli.github.com/)

    ```sh
    gh repo clone stardag-dev/stardag
    cd stardag/lib/stardag-examples
    ```

And install the package (with `ml-pipeline` extra dependencies)

=== "uv"

    ```sh
    uv sync --extra ml-pipeline
    ```

=== "pip"

    ```sh
    pip install -e ".[ml-pipeline]"
    ```

## Project Structure

The example provides a vanilla Python implementation of the ML pipeline (no DAG/data workflow framework or persistent caching used) and then the wrapping of the core business logic into Stardag, using the Class and Decorator API respectively.

```
ml_pipeline/
├── base.py           # Plain Python logic (no Stardag)
├── class_api.py      # Class-based task definitions
└── decorator_api.py  # Decorator-based task definitions
```

## Running the Examples

Just execute any of the modules as is:

=== "Active venv"

    ```sh
    uv run python -m stardag_examples.ml_pipeline.base  # | class_api | decorator_api
    ```

=== "uv run ..."

    ```sh
    python -m stardag_examples.ml_pipeline.base  # | class_api | decorator_api
    ```

## Key Concepts Demonstrated

### Deterministic Paths

The file path of any persisted result contains a hash of _all upstream dependencies_ that played a role in producing the asset. This means:

- Same parameters = same output location
- Change any upstream parameter = new output location
- Full reproducibility and cacheability

### Composability

Tasks can be composed into larger pipelines:

```{.python notest}
# Single experiment
experiment = Metrics(
    predictions=Predictions(
        trained_model=TrainedModel(model=model, dataset=train_data),
        dataset=test_data,
    )
)

# Benchmark across multiple models
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
    # ...
```

## Source Code

View the full source on GitHub: [stardag-examples/ml_pipeline](https://github.com/stardag-dev/stardag/tree/main/lib/stardag-examples/src/stardag_examples/ml_pipeline)

## Next Steps

- [Integrate with Prefect](integrate-prefect.md) - Add observability to your ML pipeline
- [Integrate with Modal](integrate-modal.md) - Run training on serverless GPUs
