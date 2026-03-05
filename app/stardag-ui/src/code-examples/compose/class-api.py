import stardag as sd

class Range(sd.Task[list[int]]):
    limit: int

    def run(self):
        self._save(list(range(self.limit)))

class Sum(sd.Task[int]):
    values: sd.TaskLoads[list[int]]

    def requires(self):
        return self.values

    def run(self):
        self._save(sum(self.values.load()))

# Declarative DAG specification - no computation yet
sum_task = Sum(values=Range(limit=4))

# Materialize all tasks' targets
sd.build(sum_task)

# Load results
assert sum_task.load() == 6
# Natural access to upstream dependencies
assert sum_task.values.load() == [0, 1, 2, 3]
