import stardag as sd

# Configure via environment variables
# STARDAG_TARGET_ROOTS__DEFAULT=s3://my-bucket/stardag

@sd.task
def my_task() -> dict:
    return {"result": 42}

# Targets are stored based on the configured root
sd.build(my_task())

# -- hidden --
assert my_task().load() == {"result": 42}
