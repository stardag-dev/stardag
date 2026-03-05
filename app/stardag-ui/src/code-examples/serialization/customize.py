import stardag as sd
from stardag.target.serialize import PandasParquetSerializer

class MyDataset(sd.Task[pd.DataFrame]):
    source: str
    # Use Parquet instead of the default serializer
    serializer = PandasParquetSerializer()

    def run(self):
        df = pd.read_csv(self.source)
        self._save(df)

dataset = MyDataset(source="data.csv")
sd.build(dataset)
dataset.load()  # Returns a pd.DataFrame read from .parquet
