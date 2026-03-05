import stardag as sd
from stardag.integration.prefect import as_flow

@sd.task
def extract(source: str) -> list[dict]:
    ...

@sd.task
def transform(data: sd.Depends[list[dict]]) -> list[dict]:
    ...

pipeline = transform(data=extract(source="events"))

# Wrap a stardag DAG as a Prefect flow
flow = as_flow(pipeline, name="etl-pipeline")
flow()  # Run as a Prefect flow with full observability
