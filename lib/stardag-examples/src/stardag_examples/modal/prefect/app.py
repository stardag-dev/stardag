import modal
import stardag.integration.modal as sd_modal

base_image = modal.Image.debian_slim(python_version="3.12")

# Define the Modal image with Stardag installed
image = sd_modal.with_stardag_on_image(
    modal.Image.debian_slim(python_version="3.12").pip_install(
        "prefect>=3.0.3",
        "pandas",
        "scikit-learn",
        "numpy",
    )
).add_local_python_source("stardag_examples")

app = sd_modal.StardagApp(
    "stardag-examples-prefect",
    builder_type="prefect",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            # Contains PREFECT_API_KEY and PREFECT_API_URL
            modal.Secret.from_name("prefect-api-key"),
            # Contains STARDAG_API_KEY
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
