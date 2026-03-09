"""Use the decorator API to turn type-hinted 
functions into tasks and compose them naturally."""
import stardag as sd

@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))

# Use `sd.Depends` to declare dependencies on other tasks' outputs
@sd.task
def get_sum(values: sd.Depends[list[int]]) -> int:
    return sum(values)

# Declarative DAG specification - no computation yet
sum_task = get_sum(values=get_range(limit=4))

# Materialize all tasks' targets
sd.build(sum_task)

# Load results
assert sum_task.load() == 6
# Natural access to upstream dependencies
assert sum_task.values.load() == [0, 1, 2, 3]
