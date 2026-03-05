"""Task target URIs are (by default) constructed by a recursive hash
of input parameters."""
import stardag as sd

@sd.task
def fetch_data(source: str) -> list[dict]:
    return [{"name": source, "score": i * 0.25} for i in range(5)]

@sd.task
def transform(data: sd.Depends[list[dict]], threshold: float) -> list[dict]:
    return [row for row in data if row["score"] >= threshold]

@sd.task
def report(data: sd.Depends[list[dict]]) -> str:
    return f"Processed {len(data)} rows"

# Same building blocks, different parameters → different output paths
users_report, events_report = [
    report(
        data=transform(
            data=fetch_data(source=source),
            threshold=0.5
        )
    ) for source in ["users", "events"]
]
assert users_report.target().uri != events_report.target().uri

sd.build([users_report, events_report])

# -- hidden --
pipeline = report(
    data=transform(data=fetch_data(source="test"), threshold=0.5)
)
sd.build(pipeline)
assert "Processed" in pipeline.load()
