import modal
from stardag.integration.modal import StardagApp, FunctionSettings

# User defines their image with full control
image = (
    modal.Image.debian_slim()
    .pip_install("stardag", "scikit-learn")
)

# Create app with builder and worker settings
stardag_app = StardagApp(
    "ml-training",
    builder_settings=FunctionSettings(image=image),
    worker_settings={
        "default": FunctionSettings(image=image),
        "gpu": FunctionSettings(image=image, gpu="A10G"),
    },
)

# After deployment, build tasks remotely:
# stardag_app.build_spawn(train_model(epochs=100, lr=0.001))

# -- hidden --
# Verify imports and construction work
assert stardag_app is not None
