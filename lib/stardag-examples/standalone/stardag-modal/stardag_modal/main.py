import sys

import modal
import stardag as sd
import stardag.integration.modal as sd_modal


@sd.task(name="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))


@sd.task(name="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)


# Must match local Python version for Modal serialization compatibility
python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

# Define the Modal image
image = (
    modal.Image.debian_slim(python_version=python_version)
    .uv_sync()  # installs *dependencies* using UV but not `stardag_modal` itself.
    .add_local_python_source("stardag_modal")  # adds `stardag_modal` source
)

# Define the StardagApp
app = sd_modal.StardagApp(
    "stardag-poc",
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

root_task = get_sum(integers=get_range(limit=5))

if __name__ == "__main__":
    res = app.build_spawn(root_task)
    print(res)
