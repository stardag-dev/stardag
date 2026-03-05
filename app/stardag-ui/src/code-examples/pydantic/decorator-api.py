import stardag as sd

@sd.task
def get_range(limit: int) -> list[int]:
    return list(range(limit))

@sd.task
def get_sum(values: sd.Depends[list[int]]) -> int:
    return sum(values)

sum_task = get_sum(values=get_range(limit=4))

# Tasks are Pydantic models — full serialization out of the box
print(sum_task.model_dump_json(indent=2))
# {
#   "values": {
#     "__name": "get_range",
#     "limit": 4
#   }
# }

# -- hidden --
import json

data = json.loads(sum_task.model_dump_json())
assert "values" in data
assert data["values"]["limit"] == 4
