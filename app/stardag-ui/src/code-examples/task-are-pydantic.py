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

print(sum_task.model_dump_json(indent=2))
# {
#   "__namespace": "",
#   "__name": "Sum",
#   "version": "",
#   "values": {
#     "__namespace": "",
#     "__name": "Range",
#     "version": "",
#     "limit": 4
#   }
# }