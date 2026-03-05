import stardag as sd

# Configure via environment variables
# STARDAG_TARGET_ROOT=s3://my-bucket/stardag
# STARDAG_TARGET_ROOT_KEY=production

@sd.task
def my_task() -> dict:
    return {"result": 42}

# Targets are stored based on the configured root
sd.build(my_task())
