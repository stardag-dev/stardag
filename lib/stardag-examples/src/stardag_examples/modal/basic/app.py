import os

import modal
import stardag.integration.modal as sd_modal
from stardag.config import config_provider
from stardag.registry._base import get_git_commit_hash

VOLUME_NAME = "stardag-default"
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# Use local stardag source for development, PyPI for production
USE_LOCAL_STARDAG = os.environ.get("STARDAG_USE_LOCAL_SOURCE", "").lower() in (
    "1",
    "true",
)


def get_profile_env_vars() -> dict[str, str]:
    """Read active profile and return env vars for Modal."""
    config = config_provider.get()
    return {  # type: ignore
        "STARDAG_REGISTRY_URL": config.api.url,
        "STARDAG_WORKSPACE_ID": config.context.workspace_id,
        "STARDAG_ENVIRONMENT_ID": config.context.environment_id,
        "COMMIT_HASH": get_git_commit_hash(),
    }


base_image = modal.Image.debian_slim(python_version="3.12")
env_vars = {
    "STARDAG_TARGET_ROOTS__DEFAULT": f"modalvol://{VOLUME_NAME}/root/default",
    **get_profile_env_vars(),
}
if USE_LOCAL_STARDAG:
    # Development mode: use local stardag source
    image = (
        base_image.pip_install(
            "modal>=1.0.0",
            "pydantic>=2.8.2",
            "pydantic-settings",
            "uuid6",
            "aiofiles>=23.1.0",
            "httpx>=0.27.0",
        )
        .env(env_vars)
        # .add_local_python_source("stardag")
        .add_local_python_source("stardag", "stardag_examples")
    )
else:
    # Production mode: install from PyPI
    image = (
        base_image.pip_install("stardag[modal]>=0.1.0")
        .env(env_vars)
        .add_local_python_source("stardag_examples")
    )

stardag_app = sd_modal.StardagApp(
    "stardag-examples-basic",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            modal.Secret.from_name("stardag-api-key"),  # NOTE optional
        ],
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image),
    },
)

app = stardag_app.modal_app
