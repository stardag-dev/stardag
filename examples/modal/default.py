import modal

import stardag as sd
import stardag.integration.modal as sd_modal


class AppContainer:
    def __init__(self):
        self.modal_app = modal.App("stardag-default")


app_container = AppContainer()

# volume_default = modal.Volume.from_name("stardag-default", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "pydantic>=2.8.2",
        "pydantic-settings>=2.7.1",
        "uuid6>=2024.7.10",
    )
    # .env({"STARDAG_TARGET_ROOT__DEFAULT": "/data/root-default"})
    .add_local_python_source("stardag")
)


def _run(task_json: str):
    sd_modal.modal_run_worker(task_json)


task_runner = sd_modal.ModalTaskRunner(
    app=app_container.modal_app,
    default_runner=app_container.modal_app.function(image=image)(_run),
)


def _build(task_json: str):
    sd_modal.modal_build_worker(task_json, task_runner)


build = app_container.modal_app.function(image=image)(_build)


# TODO wrap app creation in
# `ModalStardagApp(worker_image, build_image=None, volumes=None)`


@sd.task
def add(a: float, b: float) -> float:
    return a + b


@sd.task
def multiply(a: float, b: float) -> float:
    return a * b


@sd.task
def subtract(a: float, b: float) -> float:
    return a - b


@app_container.modal_app.local_entrypoint()
def main():
    expression = add(
        a=add(a=1, b=2),
        b=subtract(
            a=multiply(a=3, b=4),
            b=5,
        ),
    )
    sd_modal.modal_build_entrypoint(expression, build)
