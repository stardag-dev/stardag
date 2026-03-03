import stardag as sd


@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))


@sd.task
def get_sum(integers: sd.Depends[list[int]]) -> int:
    return sum(integers)


# Declarative DAG specification - no computation yet
sum_task = get_sum(integers=get_range(limit=4))

# Materialize all tasks' targets
sd.build(sum_task)

# Load results
assert sum_task.load() == 6
assert sum_task.integers.load() == [0, 1, 2, 3]
