# Stardag Examples

Example DAGs and integrations demonstrating Stardag's capabilities.

## Examples

| Example                                              | Description                                                |
| ---------------------------------------------------- | ---------------------------------------------------------- |
| [ML Pipeline](src/stardag_examples/ml_pipeline/)     | Canonical ML pipeline with train/predict/evaluate workflow |
| [Prefect Integration](src/stardag_examples/prefect/) | Build DAGs with Prefect for observability                  |
| [Modal Integration](src/stardag_examples/modal/)     | Run tasks on Modal's serverless infrastructure             |
| [General](src/stardag_examples/general/)             | Task API demonstrations and registry assets                |
| [Benchmarks](src/stardag_examples/benchmarks/)       | Performance benchmarks for build configurations            |

## Getting Started

Install with extras for the examples you want to run:

```bash
cd lib/stardag-examples

# ML Pipeline example
uv sync --extra ml-pipeline

# Prefect integration
uv sync --extra prefect --extra ml-pipeline

# Modal integration
uv sync --extra modal
```

## Documentation

For detailed guides, see the [How-To Guides](https://docs.stardag.com/how-to/) in the Stardag documentation:

- [Integrate with Prefect](https://docs.stardag.com/how-to/integrate-prefect/) - Orchestration and observability
- [Integrate with Modal](https://docs.stardag.com/how-to/integrate-modal/) - Serverless execution
- [ML Pipeline Example](https://docs.stardag.com/how-to/ml-pipeline-example/) - Complete walkthrough

## Running Examples

Examples use relative imports and should be run from this directory:

```bash
cd lib/stardag-examples
uv run python -m stardag_examples.<module>.<submodule>
```

See individual example READMEs for specific instructions.
