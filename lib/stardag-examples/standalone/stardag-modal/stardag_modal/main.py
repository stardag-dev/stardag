import modal
import stardag as sd
import stardag.integration.modal as sd_modal


@sd.task(name="Range")
def get_range(limit: int) -> list[int]:
    return list(range(limit))


@sd.task(name="Sum")
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)


# Define the Modal image
# image = (
#     modal.Image.debian_slim(python_version="3.12")
#     .uv_sync()
#     .add_local_python_source("stardag_modal")
# )

# Define the Modal image with Stardag installed
image = sd_modal.with_stardag_on_image(
    modal.Image.debian_slim(python_version="3.14")
).add_local_python_source("stardag_modal")


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

if __name__ == "__main__":
    dag = get_sum(integers=get_range(limit=21))
    res = app.build_spawn(dag)
    print(res)
