# Parameter Hashing

Parameter hashing gives each task instance a unique, deterministic identifier based on its parameters.

## Why Hashing?

Parameter hashing solves several problems:

1. **Deterministic IDs**: Same parameters always produce the same task ID
2. **Unique paths**: Each configuration gets its own output location
3. **Caching**: Re-running with same parameters reuses existing outputs
4. **Composition**: Upstream task IDs are included in downstream hashes

## The Task ID

Every task has a `task_id` property:

```python
@sd.task
def add(a: int, b: int) -> int:
    return a + b

task = add(a=1, b=2)
print(task.task_id)
# 'a1b2c3d4e5f6...'  (SHA-1 hash)
```

The task ID is derived from:

- Task family (class name or function name)
- Task namespace
- Task version
- All parameter values (recursively hashed)

## How Hashing Works

### Simple Parameters

Scalar values are included directly in the hash:

```python
task = add(a=1, b=2)

print(task._id_hash_jsonable())
# {
#     'namespace': '',
#     'family': 'add',
#     'parameters': {'version': None, 'a': 1, 'b': 2}
# }
```

### Task Parameters

When a parameter is itself a task, its task ID is used:

```python
@sd.task
def multiply(x: sd.Depends[int], factor: int) -> int:
    return x * factor

task = multiply(x=add(a=1, b=2), factor=3)

print(task._id_hash_jsonable())
# {
#     'namespace': '',
#     'family': 'multiply',
#     'parameters': {
#         'version': None,
#         'x': 'a1b2c3d4...',  # Task ID of add(1, 2)
#         'factor': 3
#     }
# }
```

This recursive hashing ensures that:

- Changes to upstream parameters change downstream IDs
- The full DAG lineage is captured in the hash

## Output Paths

The task ID determines the output path:

```python
task = add(a=1, b=2)
print(task.output().path)
# /path/to/.stardag/target-roots/default/add/a1/b2/a1b2c3d4....json
```

The path structure is:

```
<target_root>/<family>/<id[0:2]>/<id[2:4]>/<id>.json
```

The `id[0:2]/id[2:4]` directory structure prevents having too many files in a single directory.

## Customizing Hashing

### Excluding Parameters

Use `IDHashInclude` to exclude parameters from the hash:

```python
from stardag import IDHashInclude

@sd.task
def process(
    data: sd.Depends[list[int]],
    debug: Annotated[bool, IDHashInclude(False)] = False
) -> int:
    if debug:
        print("Processing...")
    return sum(data)
```

The `debug` parameter won't affect the task ID.

<!-- TODO: Verify IDHashInclude syntax and add more examples -->

### Custom Hash Functions

For complex objects, implement custom hashing:

<!-- TODO: Document IDHasher and custom hash functions -->

## Implications

### Same Parameters = Same Output Location

```python
task1 = add(a=1, b=2)
task2 = add(a=1, b=2)

assert task1.task_id == task2.task_id
assert task1.output().path == task2.output().path
```

### Parameter Changes = New Output Location

```python
task1 = add(a=1, b=2)
task2 = add(a=1, b=3)  # Different 'b'

assert task1.task_id != task2.task_id
```

### Version Changes = New Output Location

```python
@sd.task(version="1")
def compute(x: int) -> int:
    return x

@sd.task(version="2")
def compute_v2(x: int) -> int:
    return x * 2

# Different versions = different task IDs
```
