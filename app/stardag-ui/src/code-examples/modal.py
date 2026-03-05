"""Scale out with Modal workers."""
import modal
from modal import gpu
import stardag as sd
from stardag.integration import modal as sd_modal

# Define your DAG as usual
@sd.task
def prepare_data() -> str:
    # ... data preparation logic ...
    return "prepared_data"

@sd.task
def train_model(prepared_data: str) -> str:
    # ... training logic ...
    return "trained_model"

pipeline = train_model(prepared_data=prepare_data())

# Define your Modal build and worker settings
image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_sync(extras=["ml-pipeline"])
    .add_local_python_source("stardag_examples")
)
app = sd_modal.StardagApp(
    "ml-pipeline",
    builder_settings=sd_modal.FunctionSettings(
        image=image,
        secrets=[
            # optional for communication with registry
            modal.Secret.from_name("stardag-api-key"),
        ],
    ),
    worker_settings={
        "default": sd_modal.FunctionSettings(
            image=image,
            cpu=1,
        ),
        "gpu-large": sd_modal.FunctionSettings(
            image=image,
            gpu=gpu.A100(count=4),
        ),
    },
)

# Flexibly route tasks to different workers based on any task properties
def worker_selector(task: sd.BaseTask) -> str:
    if task.get_name() == "TrainedModel":
        return "gpu-large"
    return "default"

# Submit the pipeline to Modal
app.build_spawn(pipeline, worker_selector=worker_selector)
