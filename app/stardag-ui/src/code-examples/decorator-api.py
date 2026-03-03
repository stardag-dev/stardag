import stardag as sd

@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task
def get_sum(values: sd.Depends[list[int]]) -> int:
    return sum(values)

# Declarative DAG specification - no computation yet
sum_task = get_sum(values=get_range(limit=4))

# Materialize all tasks' targets
sd.build(sum_task)  # Materialize all tasks' targets

assert sum_task.load() == 6  # Load results
assert sum_task.values.load() == [0, 1, 2, 3]  # access dependencies