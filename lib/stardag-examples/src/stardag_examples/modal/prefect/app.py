import sys

import modal
import stardag.integration.modal as sd_modal

# Must match local Python version for Modal serialization compatibility
python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

# Define the Modal image
image = (
    modal.Image.debian_slim(python_version=python_version)
    .uv_sync(extras=["ml-pipeline", "prefect"])
    .add_local_python_source("stardag_examples")
)

# v0.5.5+: ``builder_type="prefect"`` is gone. Use the
# ``PrefectBuilder`` class re-exported from
# ``stardag.integration.modal`` (or subclass ``Builder`` for custom
# logic).
app = sd_modal.StardagApp(
    "stardag_examples-prefect",
    # Declared so a reactive scheduler tick can rebuild these tasks from
    # registry data rather than from the build task store's pickles, which
    # need target-root write access at trigger time and are invalidated by a
    # redeploy. Inference alone only warns; declaring is what enables it.
    task_modules=["stardag_examples.ml_pipeline.*"],
    build_function=sd_modal.PrefectBuilder(),
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            # Contains PREFECT_API_KEY and PREFECT_API_URL
            modal.Secret.from_name("prefect-api"),
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
