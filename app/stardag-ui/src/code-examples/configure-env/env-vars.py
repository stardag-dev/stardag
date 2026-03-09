"""Configure storage centrally. Easily swap between local/dev and
remote/production storage.

Configure via environment variables:
export STARDAG_TARGET_ROOTS__DEFAULT=s3://my-bucket/stardag
export STARDAG_TARGET_ROOTS__INGESTION=s3://my-other-bucket/stardag
"""
import stardag as sd

@sd.task(target_root_key="ingestion")
def ingest(source: str) -> dict[str, str]:
    return {"result": source}

@sd.task  # default target_root_key is "default"
def process(data: sd.Depends[dict[str, str]]) -> dict[str, str]:
    return {"result": data["result"] + " processed"}


# Targets are stored based on the configured root
sd.build(process(data=ingest(source="42")))
