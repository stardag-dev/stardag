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

sd.build(sum_task)  # Materialize all tasks' targets

assert sum_task.load() == 6  # Load results
assert sum_task.values.load() == [0, 1, 2, 3]  # access dependencies
