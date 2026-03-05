import stardag as sd

@sd.task
def train_model(
    dataset: sd.Depends[list[dict]],
    learning_rate: float = 0.01,
    epochs: int = 10,
    # Exclude verbose from the hash — doesn't affect output
    verbose: sd.Annotated[bool, sd.StardagField(exclude=True)] = False,
) -> dict:
    ...

# These two produce the same output path (verbose is excluded)
task_a = train_model(dataset=..., learning_rate=0.01, verbose=True)
task_b = train_model(dataset=..., learning_rate=0.01, verbose=False)
assert task_a.task_id() == task_b.task_id()
