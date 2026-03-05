import pandas as pd
import stardag as sd
from stardag.target.serialize import PandasDataFrameCSVSerializer

class MyDataset(sd.Task[pd.DataFrame]):
    source: str
    # Use CSV serializer instead of the default
    _serializer = PandasDataFrameCSVSerializer()

    def run(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        self._save(df)

dataset = MyDataset(source="data.csv")
sd.build(dataset)
dataset.load()  # Returns a pd.DataFrame read from CSV

# -- hidden --
assert isinstance(dataset.load(), pd.DataFrame)
assert list(dataset.load().columns) == ["a", "b"]
