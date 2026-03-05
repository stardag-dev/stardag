import stardag as sd

@sd.task
def fetch_data(source: str) -> list[dict]:
    ...

@sd.task
def transform(data: sd.Depends[list[dict]], threshold: float) -> list[dict]:
    return [row for row in data.load() if row["score"] >= threshold]

@sd.task
def report(data: sd.Depends[list[dict]]) -> str:
    return f"Processed {len(data.load())} rows"

# Same building blocks, different parameters → different output paths
for source in ["users", "events"]:
    pipeline = report(
        data=transform(data=fetch_data(source=source), threshold=0.5)
    )
    sd.build(pipeline)
