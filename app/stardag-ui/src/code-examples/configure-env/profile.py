"""Configure storage centrally. Easilly swap between local/dev and 
remote/production storage.

Use configured profiles -> target roots stored centrally in the registry:

# ~/.stardag/config.toml
[registry.central]
url = "https://api.stardag.com"

[profile.my-workspace_personal]
registry = "central"
workspace = "my-workspace"
environment = "personal"
user = "me@mail.com"

```sh
stardag config profile use my-workspace_personal
```
"""
import stardag as sd

@sd.task(target_root_key="ingestion")
def ingest(source: str) -> dict:
    return {"result": source}

@sd.task  # default target_root_key is "default"
def process(data: sd.Depends[dict]) -> dict:
    return {"result": data["result"] + " processed"}


# Targets are stored based on the configured root
sd.build(process(data=ingest(source="42")))
