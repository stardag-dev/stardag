import modal
import stardag.integration.modal as sd_modal

base_image = modal.Image.debian_slim(python_version="3.12")

# Define the Modal image with Stardag installed
image = sd_modal.with_stardag_on_image(
    modal.Image.debian_slim(python_version="3.12").pip_install(
        # helper to pull in all dependencies of current package (stardag-examples)
        # including optional "ml-pipeline" deps
        *sd_modal.get_package_deps(__file__, optional=["ml-pipeline"]),
    )
).add_local_python_source("stardag_examples")

app = sd_modal.StardagApp(
    "stardag_examples-ml_pipeline",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            # required for communication with registry
            modal.Secret.from_name("stardag-api-key"),
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
