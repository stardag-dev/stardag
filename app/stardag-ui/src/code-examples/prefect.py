"""Prefect lacks Makefile-style bottom up execution, but we can easilly 
build stardag DAGs as part of any prefect flow or task."""
from prefect import flow
import stardag as sd
from stardag.integration import prefect as sd_prefect

@sd.task
def extract(source: str) -> list[dict]:
    return [{"source": source, "value": i} for i in range(3)]

@sd.task
def transform(data: sd.Depends[list[dict]]) -> list[dict]:
    return [row for row in data if row["value"] > 0]

@flow
async def build_flow(pipeline: sd.Task):
    # Run any other prefect code ...
    dag = transform(data=extract(source="events"))
    await sd_prefect.build_aio(dag)
    return dag.load()