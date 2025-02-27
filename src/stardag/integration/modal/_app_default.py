import modal

# from pydantic import TypeAdapter

# import stardag as sd
# from stardag.build.task_runner import TaskRunner

app_defult = modal.App("stardag-default")
volume_default = modal.Volume.from_name("stardag-default", create_if_missing=True)
