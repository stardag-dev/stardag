# Exceptions

Stardag exceptions for error handling.

## Exception Hierarchy

```
StardagError
├── APIError
│   ├── AuthenticationError
│   ├── AuthorizationError
│   ├── SDKVersionUnsupportedError
│   └── TokenExpiredError
├── ResumableInterruption
└── ...
```

## Base Exception

### StardagError

```python
from stardag import StardagError
```

Base exception for all Stardag errors.

```python
try:
    sd.build(task)
except StardagError as e:
    print(f"Stardag error: {e}")
```

## API Exceptions

### APIError

```python
from stardag import APIError
```

Base exception for API-related errors.

### AuthenticationError

```python
from stardag import AuthenticationError
```

Raised when authentication fails:

- Invalid credentials
- Missing API key
- OAuth flow failure

**Handling:**

```python
try:
    registry = APIRegistry()
except AuthenticationError:
    print("Please login: stardag auth login")
```

### AuthorizationError

```python
from stardag import AuthorizationError
```

Raised when authenticated but not authorized:

- Insufficient permissions
- Wrong workspace/environment
- Resource access denied

### SDKVersionUnsupportedError

```python
from stardag import SDKVersionUnsupportedError
```

Raised when the registry refuses the request because this SDK is older than
the minimum version that registry supports (HTTP `426 Upgrade Required`).

Every request the SDK makes carries its version in an
`X-Stardag-SDK-Version` header, which is what lets a registry answer this
way at all. Nothing is enforced unless the registry is configured with a
minimum; by default any SDK version is accepted.

`message` is the server's own sentence — it names both versions and the
exact upgrade command — and `sdk_version` / `minimum_sdk_version` carry the
same two versions for programmatic use:

```python
try:
    sd.build(task, registry=registry)
except SDKVersionUnsupportedError as e:
    print(e.message)  # e.g. 'pip install --upgrade "stardag>=X"'
    print(e.sdk_version, "->", e.minimum_sdk_version)
```

The reverse direction — a **new** SDK against an **old** registry — is not a
supported combination; upgrade both together. It surfaces as a clear
"this registry does not support …, upgrade stardag-api" error from whichever
command needs an endpoint the registry does not have.

### TokenExpiredError

```python
from stardag import TokenExpiredError
```

Raised when authentication token has expired:

**Handling:**

```python
try:
    sd.build(task, registry=registry)
except TokenExpiredError:
    # Refresh token and retry
    os.system("stardag auth refresh")
```

## ResumableInterruption

```python
from stardag import ResumableInterruption
```

The one exception you **raise** rather than catch. It says: _I saved my
progress, run me again._

```python
import stardag as sd
from stardag.integration.modal import MODAL_INTERRUPTIONS


class TrainModel(sd.Task[None]):
    def target(self) -> sd.DirectoryTarget:
        return sd.get_directory_target(sd.get_default_relpath(self))

    def run(self):
        checkpoint = self.target() / "checkpoint.json"
        try:
            train(resume_from=checkpoint)
        except MODAL_INTERRUPTIONS:        # preemption OR the function timeout
            save_checkpoint(checkpoint)
            raise sd.ResumableInterruption("checkpointed") from None
        self.target().mark_done()
```

**An interruption you do not catch is a failure**, deliberately. Letting
one propagate means the task had no plan for it — it hung, or the worker's
timeout is too small — and both want the same answer: fail, under the
scheduler's ordinary attempt budget. So there is no setting anywhere
deciding whether a timeout was "expected"; the task answers by raising
this, or by not raising it.

!!! danger "Catch the interruption types, never `BaseException`"

    A `NameError` is a `BaseException` too. A blanket catch sweeps up
    ordinary bugs, and re-raising `ResumableInterruption` for one turns a
    deterministic failure into a task that resumes until its budget runs
    out. Use `MODAL_INTERRUPTIONS` (exactly `KeyboardInterrupt` and
    `modal.exception.InputCancellation`).

    `except KeyboardInterrupt:` is wrong the other way: `InputCancellation`
    is not a `KeyboardInterrupt`, so it misses timeouts entirely.

`ResumableInterruption` is an ordinary `Exception`, not a `BaseException`:
you raise it from inside your own error handling, where a `BaseException`
subclass would be one more thing slipping past your control flow. The Modal
runner catches it and re-raises an interrupt in its place, so the backend
still sees a container to restart.

Resumption is bounded by `TickConfig.max_interruptions` (default 20), a
budget separate from `max_attempts` — see
[Preemption and timeouts](../how-to/integrate-modal.md#preemption-and-timeouts).

## Common Error Scenarios

### Target Root Not Configured

```python
# Error: No target root configured for 'default'
# Solution:
export STARDAG_TARGET_ROOTS__DEFAULT=/path/to/outputs
```

### Task Not Complete

```python
# A dependency failed to build
try:
    sd.build(task)
except Exception as e:
    # Check task completion status
    print(task.complete())  # False
```

### Serialization Error

```python
# Output type cannot be serialized
# Ensure return type is JSON-serializable or use pickle
@sd.task
def my_task() -> dict:  # JSON-serializable
    return {"key": "value"}
```

## Best Practices

1. **Catch specific exceptions** - Handle `AuthenticationError` differently from `APIError`
2. **Log error details** - Exceptions contain useful debugging info
3. **Graceful degradation** - Fall back to local builds if API unavailable

```python
from stardag import APIError, AuthenticationError

try:
    sd.build(task, registry=registry)
except AuthenticationError:
    print("Auth failed - running locally")
    sd.build(task)
except APIError as e:
    print(f"API error: {e} - running locally")
    sd.build(task)
```

<!-- TODO: Document additional exception types as SDK evolves -->
