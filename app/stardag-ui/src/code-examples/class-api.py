import stardag as sd


class GetRange(sd.Task[list[int]]):
    limit: int

    def run(self):
        self._save(list(range(self.limit)))


class GetSum(sd.Task[int]):
    integers: sd.TaskLoads[list[int]]

    def requires(self):
        return self.integers

    def run(self):
        self._save(sum(self.integers.load()))


# Declarative DAG specification - no computation yet
sum_task = GetSum(integers=GetRange(limit=4))

# Materialize all tasks' targets
sd.build(sum_task)

# Load results
assert sum_task.load() == 6
assert sum_task.integers.load() == [0, 1, 2, 3]
