"""Example Stardag Modal app definition.

This module demonstrates how to define a StardagApp for Modal deployment.

Key concepts:
1. User has control over the image definition (`sd_modal.with_stardag_on_image` just
pip install latest version of Stardag, or local source if on local dev version)
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

import modal
import stardag.integration.modal as sd_modal

# Define the Modal image with Stardag installed
image = sd_modal.with_stardag_on_image(
    modal.Image.debian_slim(python_version="3.12")
).add_local_python_source("stardag_examples")

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
