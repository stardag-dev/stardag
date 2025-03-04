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

## Define your stardag-modal-app

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
