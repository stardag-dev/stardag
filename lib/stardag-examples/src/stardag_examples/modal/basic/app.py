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

import sys

import modal
import stardag.integration.modal as sd_modal

# Must match local Python version for Modal serialization compatibility
python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

# Define the Modal image
image = (
    modal.Image.debian_slim(python_version=python_version)
    .uv_sync()
    .add_local_python_source("stardag_examples")
)

# Optionally use this instead to use local source for stardag itself.
# image = sd_modal.with_stardag_on_image(
#     modal.Image.debian_slim(python_version=python_version).pip_install(
#         # helper to pull in all dependencies of current package (stardag-examples)
#         *sd_modal.get_package_deps(__file__),
#     )
# ).add_local_python_source("stardag_examples")


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
