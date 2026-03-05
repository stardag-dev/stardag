import stardag as sd

sd.configure(
    target_root="s3://my-bucket/stardag",
    target_root_key="production",
)

@sd.task
def my_task() -> dict:
    return {"result": 42}

# Targets are now stored in S3
sd.build(my_task())
