"""Example Stardag Modal app definition.

This module demonstrates how to define a StardagApp for Modal deployment.

Key concepts:
1. User has control over the image definition. `uv_sync` installs dependencies but not
    the local source, which is added separately with `add_local_python_source`.
2. Local sources should be added LAST for layer caching
3. Profile environment variables are injected by the CLI at deploy time

Usage:
    # Deploy with active profile
    stardag modal deploy app.py

    # Deploy with specific profile
    stardag modal deploy app.py --profile production

    # Run tasks after deployment
    python main.py
"""

import modal
import stardag.integration.modal as sd_modal

# Define the Modal image
image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_sync()
    .add_local_python_source("stardag_examples")
)

# Define the StardagApp
app = sd_modal.StardagApp(
    "stardag_examples-basic",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            # required for communication with registry
            modal.Secret.from_name("stardag-api-key"),
        ],
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(image=image),
    },
)
