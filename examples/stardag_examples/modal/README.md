# `*DAG` Examples: Modal Integration

[Modal](https://modal.com/docs) is an excellent choice for materializing your \*DAGs:

> Modal provides a serverless cloud for engineers and researchers who want to build compute-intensive applications without thinking about infrastructure.
>
> Run generative AI models, large-scale batch workflows, job queues, and more, all faster than ever before.

To do so, follow these steps:

## Prerequisites

Create a modal account and log in.

It is recommended that you keep you stardag-modal code in a proper package structure, with a top level package with an `__init__.py` file, and that you separate the code defining your stardag-modal app from the stardag tasks, e.g.:

```
my_package/
  __init__.py  # (can be empty)
  app.py  # app definition
  task.py  # task definitions
  ...
```

## Basics

This section corresponds to what is found in `stardag_examples.modal.basic`.

TL;DR/to just run the example:

```sh
modal deploy stardag_examples/modal/basic/app.py
python stardag_examples/modal/basic/main.py
```

### Define your stardag-modal-app

```python
# my_package/app.py

import modal

import stardag.integration.modal as sd_modal

VOLUME_NAME = "stardag-default"
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("stardag[modal]>=0.0.3")
    .env(
        {
            "STARDAG_TARGET_ROOT__DEFAULT": f"modalvol://{VOLUME_NAME}/root/default",
        }
    )
    .add_local_python_source(
        "stardag_examples",
    )
)

stardag_app = sd_modal.StardagApp(
    "stardag-examples-basic",
    builder_settings=sd_modal.FunctionSettings(image=image),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image),
    },
)

app = stardag_app.modal_app
```

We have now defined a modal app named `stardag-examples-basic` with two modal functions. Let's deploy it:

```
modal deploy my_package/app.py
```

Now you should see something like this in [modal.com/apps](https://modal.com/apps)

<img width="424" alt="image" src="https://github.com/user-attachments/assets/38be17de-5d3c-4c7d-a0ea-94cfbb7b37ff" />

### Define your tasks

For a minimal example

```python
# my_package/task.py
import stardag as sd


@sd.task(family="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))


@sd.task(family="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)
```

### Build a DAG of your tasks with the modal app

There are a few options here, but typically you want to "submit" a DAG for materialization:

```python
# my_package/main.py
from stardag_examples.modal.basic.app import stardag_app
from stardag_examples.modal.basic.task import get_range, get_sum

if __name__ == "__main__":
    dag = get_sum(integers=get_range(limit=10))
    stardag_app.build_spawn(dag)
```

You should see the `build` and `worker_default` functions being invoked in the modal UI.

To retrive the output locally, you need to configure the same "target root" as is used in the app (see the image `.env` statement in the [first step](#define-your-stardag-modal-app))

```sh
export STARDAG_TARGET_ROOT__DEFAULT=modalvol://stardag-default/root/default
```

and run

```python

from stardag_examples.modal.basic.task import get_range, get_sum

dag = get_sum(integers=get_range(limit=10))
dag.output().load()
# 45
```

## A more interesting example: Run tasks in parallel on different worker types...

...with `prefect` for observability.

TL;DR/to just run the example:

```sh
modal deploy stardag_examples/modal/prefect/app.py
python stardag_examples/modal/prefect/main.py
```
