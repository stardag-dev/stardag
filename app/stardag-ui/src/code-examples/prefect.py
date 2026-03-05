import stardag as sd
from stardag.integration.prefect import build_flow

@sd.task
def extract(source: str) -> list[dict]:
    return [{"source": source, "value": i} for i in range(3)]

@sd.task
def transform(data: sd.Depends[list[dict]]) -> list[dict]:
    return [row for row in data if row["value"] > 0]

pipeline = transform(data=extract(source="events"))

# build_flow is a Prefect @flow that builds any stardag DAG
# await build_flow(pipeline)  # Run as a Prefect flow with full observability

# -- hidden --
# Verify imports resolve and tasks construct correctly
assert pipeline is not None
assert build_flow is not None
