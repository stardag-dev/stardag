import modal

import stardag as sd
import stardag.integration.modal as sd_modal
from stardag.utils.testing import simple_dag

worker_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "pydantic>=2.8.2",
        "pydantic-settings>=2.7.1",
        "uuid6>=2024.7.10",
    )
    .env({"STARDAG_TARGET_ROOT__DEFAULT": "/data/root-default"})
    .add_local_python_source("stardag")
)
volume_default = modal.Volume.from_name("stardag-default", create_if_missing=True)


def worker_selector(task: sd.Task) -> str:
    return "large" if isinstance(task, simple_dag.LeafTask) else "default"


stardag_app = sd_modal.StardagApp(
    "stardag-default-2",
    builder_settings=sd_modal.FunctionSettings(
        image=worker_image,
        volumes={"/data": volume_default},
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(
            image=worker_image,
            cpu=1,
            volumes={"/data": volume_default},
        ),
        "large": sd_modal.FunctionSettings(
            image=worker_image,
            cpu=2,
            volumes={"/data": volume_default},
        ),
    },
    worker_selector=worker_selector,
)
app = stardag_app.modal_app


@app.local_entrypoint()
def main():
    dag = simple_dag.get_simple_dag()
    return stardag_app.build_remote(dag)


if __name__ == "__main__":
    dag = simple_dag.get_simple_dag()
    res = stardag_app.build_spawn(dag)
