import modal

import stardag.integration.modal as sd_modal

VOLUME_NAME = "stardag-default"

volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    # TODO replace with just stardag
    .pip_install(
        "pydantic>=2.8.2",
        "pydantic-settings>=2.7.1",
        "uuid6>=2024.7.10",
    )
    .env(
        {
            "STARDAG_TARGET_ROOT__DEFAULT": f"modalvol://{VOLUME_NAME}/root/default",
        }
    )
    .add_local_python_source(
        "stardag",
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
