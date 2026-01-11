import os

import modal
import stardag.integration.modal as sd_modal

VOLUME_NAME = "stardag-default"

volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# Use local stardag source for development, PyPI for production
USE_LOCAL_STARDAG = os.environ.get("STARDAG_USE_LOCAL_SOURCE", "").lower() in (
    "1",
    "true",
)

base_image = modal.Image.debian_slim(python_version="3.12")

common_env = {
    "PREFECT_API_URL": (  # NOTE optional
        "https://api.prefect.cloud/api/"
        "accounts/bbef4152-5db3-471d-81c9-fa01fb0e0eb8/"
        "workspaces/04af8615-4247-4607-a052-291be49f958f"
    ),
    "STARDAG_TARGET_ROOTS__DEFAULT": f"modalvol://{VOLUME_NAME}/root/default",
}

if USE_LOCAL_STARDAG:
    # Development mode: use local stardag source
    image = (
        base_image.pip_install(
            "modal>=1.0.0",
            "pydantic>=2.8.2",
            "pydantic-settings",
            "prefect>=3.0.3",
            "pandas",
            "scikit-learn",
            "numpy",
            "uuid6",
            "aiofiles>=23.1.0",
        )
        .env(common_env)
        .add_local_python_source("stardag", "stardag_examples")
    )
else:
    # Production mode: install from PyPI
    image = (
        base_image.pip_install(
            "stardag[modal,prefect]>=0.0.3",
            "pandas",
            "scikit-learn",
            "numpy",
            "uuid6",
        )
        .env(common_env)
        .add_local_python_source("stardag_examples")
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
