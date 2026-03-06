import stardag as sd

# Access the current configuration programmatically
config = sd.config_provider.get()

# Read target roots
print(config.target.roots)
# {"default": "/home/user/.stardag/target"}

# Override target roots at runtime
from stardag.target import target_factory_provider, TargetFactory

with target_factory_provider.override(
    TargetFactory(target_roots={"default": "/tmp/my-stardag-output"})
):
    @sd.task
    def my_task() -> dict:
        return {"result": 42}

    sd.build(my_task())

# -- hidden --
from stardag.target import InMemoryFileTarget, target_factory_provider, TargetFactory
from stardag.target._factory import TargetFactory as TF

# Verify config_provider works
config = sd.config_provider.get()
assert hasattr(config, "target")
assert hasattr(config.target, "roots")
