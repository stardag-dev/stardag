# Build with `prefect`

Builds a DAG using `prefect`. To run this example you must first install prefect, and ml-pipeline

```shell
uv sync --extra prefect --extra ml-pipeline
```

And have access to a prefect server, by Option 1 _or_ 2 below:

Option 1 - Use a local prefect server

```shell
prefect server start
```

then, in separate terminal:

```shell
export PREFECT_API_URL="http://127.0.0.1:4200/api"
```

Option 2 - Use Prefect Cloud

Sign up at <https://www.prefect.io/> then run:

```shell
prefect cloud login
```

Then, run:

```shell
uv run python -m stardag_examples.prefect.main
```

You should get several prefect logs, navigate to the Prefect UI, click "latest run" and
you should see something like:

<img width="1181" alt="image" src="https://github.com/user-attachments/assets/372c40c4-ca14-49b3-bbf6-18b758ddce5f">
