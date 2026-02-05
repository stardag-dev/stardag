"""Example Stardag Modal app definition.

This module demonstrates how to define a StardagApp for Modal deployment.

Key concepts:
1. User has full control over the image definition
2. Local sources should be added LAST for optimal layer caching
3. Profile environment variables are injected by the CLI at deploy time

Usage:
    # Deploy with active profile
    stardag modal deploy app.py

    # Deploy with specific profile
    stardag modal deploy app.py --profile production

    # Run tasks after deployment
    python main.py
"""

import os

import modal
import stardag.integration.modal as sd_modal

# Volume for task outputs
VOLUME_NAME = "stardag-default"
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# Use local stardag source for development, PyPI for production
USE_LOCAL_STARDAG = os.environ.get("STARDAG_USE_LOCAL_SOURCE", "").lower() in (
    "1",
    "true",
)

# Static environment variables (baked into image)
# Profile-specific variables (STARDAG_REGISTRY_URL, etc.) are injected
# by the CLI at deploy time via --profile flag
STATIC_ENV_VARS = {
    "STARDAG_TARGET_ROOTS__DEFAULT": f"modalvol://{VOLUME_NAME}/root/default",
}

# Build the image
base_image = modal.Image.debian_slim(python_version="3.12")

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
        .env(STATIC_ENV_VARS)
        # Local sources added LAST for optimal layer caching
        .add_local_python_source("stardag", "stardag_examples")
    )
else:
    # Production mode: install from PyPI
    image = (
        base_image.pip_install("stardag[modal]>=0.1.0")
        .env(STATIC_ENV_VARS)
        # Local sources added LAST for optimal layer caching
        .add_local_python_source("stardag_examples")
    )

# Create the StardagApp (functions are NOT created yet - deferred until finalize())
# The CLI will call finalize() and inject profile env vars before deployment
stardag_app = sd_modal.StardagApp(
    "stardag-examples-basic",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            modal.Secret.from_name("stardag-api-key"),  # Optional API key
        ],
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image),
    },
)
