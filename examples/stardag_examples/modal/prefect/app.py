import modal

import stardag.integration.modal as sd_modal

VOLUME_NAME = "stardag-default"

volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "stardag[modal,prefect]>=0.0.3",
        "pandas",
        "scikit-learn",
        "numpy",
    )
    .env(
        {
            "PREFECT_API_URL": (  # NOTE optional
                "https://api.prefect.cloud/api/"
                "accounts/bbef4152-5db3-471d-81c9-fa01fb0e0eb8/"
                "workspaces/04af8615-4247-4607-a052-291be49f958f"
            ),
            "STARDAG_TARGET_ROOT__DEFAULT": f"modalvol://{VOLUME_NAME}/root/default",
        }
    )
    .add_local_python_source(
        "stardag_examples",
    )
)

stardag_app = sd_modal.StardagApp(
    "stardag-examples-prefect",
    builder_type="prefect",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            modal.Secret.from_name("prefect-api-key"),  # NOTE optional
        ],
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(
            image=image,
            cpu=1,
        ),
        "large": sd_modal.FunctionSettings(
            image=image,
            cpu=2,
        ),
    },
)

app = stardag_app.modal_app
