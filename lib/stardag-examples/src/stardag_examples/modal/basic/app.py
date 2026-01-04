import modal

import stardag.integration.modal as sd_modal

VOLUME_NAME = "stardag-default"
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("stardag[modal]>=0.0.3")
    .env(
        {
            "STARDAG_TARGET_ROOTS__DEFAULT": f"modalvol://{VOLUME_NAME}/root/default",
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
