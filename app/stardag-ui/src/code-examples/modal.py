import stardag as sd
from stardag.integration.modal import ModalExecutor

@sd.task
def train_model(epochs: int, lr: float) -> dict:
    # This runs on Modal's cloud infrastructure
    ...

# Execute on Modal with a GPU
executor = ModalExecutor(gpu="A10G", timeout=3600)
sd.build(train_model(epochs=100, lr=0.001), executor=executor)
