from typing import Annotated
import stardag as sd

class GetDataset(sd.Task[list[dict]]):
    def run(self):
        self._save([{"x": 1}, {"x": 2}])

class TrainModel(sd.Task[dict]):
    dataset: sd.TaskLoads[list[dict]]
    learning_rate: float = 0.01
    epochs: int = 10
    # Exclude verbose from the hash — doesn't affect output
    verbose: Annotated[bool, sd.StardagField(hash_exclude=True)] = False

    def requires(self):
        return self.dataset

    def run(self):
        self._save({"lr": self.learning_rate, "epochs": self.epochs})

data = GetDataset()

# These two produce the same output path (verbose is excluded)
task_a = TrainModel(dataset=data, learning_rate=0.01, verbose=True)
task_b = TrainModel(dataset=data, learning_rate=0.01, verbose=False)
assert task_a.id == task_b.id
