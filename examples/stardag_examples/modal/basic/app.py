import modal

import stardag.integration.modal as sd_modal

worker_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "pydantic>=2.8.2",
        "pydantic-settings>=2.7.1",
        "uuid6>=2024.7.10",
    )
    .env(
        {
            "STARDAG_TARGET_ROOT__DEFAULT": "modalvol://stardag-default/root/default",
            # "STARDAG_MODAL_VOLUME_MOUNTS": '{"/data": "stardag-default"}',
        }
    )
    .add_local_python_source(
        "stardag",
        "stardag_examples",
    )
)
volume_default = modal.Volume.from_name("stardag-default", create_if_missing=True)

stardag_app = sd_modal.StardagApp(
    "stardag-examples-basic",
    builder_settings=sd_modal.FunctionSettings(
        image=worker_image,
        # volumes={"/data": volume_default},
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(
            image=worker_image,
            cpu=1,
            # volumes={"/data": volume_default},
        ),
        "large": sd_modal.FunctionSettings(
            image=worker_image,
            cpu=2,
            # volumes={"/data": volume_default},
        ),
    },
)

app = stardag_app.modal_app
