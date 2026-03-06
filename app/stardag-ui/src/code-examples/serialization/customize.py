"""Modular and customizable serialization."""
from typing import Annotated
import pandas as pd
import stardag as sd
from stardag.target.serialize import Serializer

class DataFrameParquetSerializer(Serializer[pd.DataFrame, sd.FileTarget]):
    def dump(self, obj: pd.DataFrame, target: sd.FileTarget):
        with target.proxy_path("w") as proxy_path:
            obj.to_parquet(proxy_path)

    def load(self, target: sd.FileTarget) -> pd.DataFrame:
        with target.proxy_path("r") as proxy_path:
            return pd.read_parquet(proxy_path)

ParquetDataFrame = Annotated[pd.DataFrame, DataFrameParquetSerializer()]

class MyDataset(sd.Task[ParquetDataFrame]):
    source: str

    def run(self):
        df = pd.DataFrame({"a": [1, 2, 3], "b": [4, 5, 6]})
        self._save(df)

dataset = MyDataset(source="data.csv")
sd.build(dataset)
df = dataset.load()

# -- hidden --
assert isinstance(df, pd.DataFrame)
assert list(df.columns) == ["a", "b"]
