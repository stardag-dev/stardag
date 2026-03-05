import stardag as sd

# Load a task specification from the registry by ID
task = sd.registry.get_task("ab12cd34-...")

# The task knows how to deserialize its own output
result = task.load()

# Or load from a JSON specification
spec = '{"__name": "Sum", "values": {"__name": "Range", "limit": 4}}'
task = sd.Task.model_validate_json(spec)
sd.build(task)
