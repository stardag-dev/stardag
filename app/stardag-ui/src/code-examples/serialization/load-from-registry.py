import stardag as sd

@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task
def get_sum(values: sd.Depends[list[int]]) -> int:
    return sum(values)

# Serialize a task DAG to JSON
task = get_sum(values=get_range(limit=4))
spec = task.model_dump_json(indent=2)
print(spec)

# Reconstruct the exact task from JSON
restored = type(task).model_validate_json(spec)
sd.build(restored)

assert restored.load() == 6

# -- hidden --
import json

data = json.loads(spec)
assert data["values"]["limit"] == 4
