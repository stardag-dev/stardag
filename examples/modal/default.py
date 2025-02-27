import modal

import stardag as sd
import stardag.integration.modal as sd_modal

modal_app = modal.App("stardag-default")
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


@modal_app.function(image=image)
def run(task_json: str):
    sd_modal.modal_run_worker(task_json)


task_runner = sd_modal.ModalTaskRunner(
    app=modal_app,
    default_runner=run,
)


@modal_app.function(image=image)
def build(task_json: str):
    sd_modal.modal_build_worker(task_json, task_runner)


@sd.task
def add(a: float, b: float) -> float:
    return a + b


@sd.task
def multiply(a: float, b: float) -> float:
    return a * b


@sd.task
def subtract(a: float, b: float) -> float:
    return a - b


@modal_app.local_entrypoint()
def main():
    expression = add(
        a=add(a=1, b=2),
        b=subtract(
            a=multiply(a=3, b=4),
            b=5,
        ),
    )
    sd_modal.modal_build_entrypoint(expression, build)
