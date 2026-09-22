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
├── ExecutionCancelled
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


class TrainModel(sd.TargetTask[sd.DirectoryTarget]):
    def target(self) -> sd.DirectoryTarget:
        return sd.get_directory_target(sd.get_default_relpath(self))

    def run(self):
        directory = self.target()
        checkpoint = directory / "checkpoint.json"
        try:
            train(resume_from=checkpoint)
        except MODAL_INTERRUPTIONS:        # preemption OR the function timeout
            save_checkpoint(checkpoint)
            raise sd.ResumableInterruption("checkpointed") from None
        directory.mark_done()
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
subclass would be one more thing slipping past your control flow.

What happens next depends on whether a restart is still possible, which the
Modal runner reads off **the interruption you caught** — it is still on the
exception you raise, and `raise ... from None` keeps it there (that form
hides the "During handling…" preamble; it does not discard the original).

Caught a preemption, the runner re-raises an interrupt in its place so the
backend sees a crashed container and restarts the input on the same call
id, and records the preemption so a restart that never arrives is visible.
Caught a function timeout or a cancel — when no restart is coming — it
records an interruption for a scheduler tick to act on instead.

So raise it **from inside the `except` block**. `raise
sd.ResumableInterruption(...) from None` keeps the link, so does the same
statement with no `from` clause, and so does an explicit `from err` on a
saved exception. (A bare `raise` is not one of these: it re-raises the
platform exception, so the runner never sees a resumption request and
records nothing — on a preemption the backend restarts the input anyway,
but on a timeout or a cancel the execution dies and a later tick records a
retryable failure.) What loses the link is raising where the interruption is no
longer reachable — outside the block with no explicit cause, or on a
condition of your own. Then stardag falls back to comparing elapsed time
against the worker's declared `timeout`, which is a guess on a clock that
starts after the container does.

Resumption is bounded by `TickConfig.max_interruptions` (default 20), a
budget separate from `max_attempts` — see
[Preemption and timeouts](../how-to/integrate-modal.md#preemption-and-timeouts).

## ExecutionCancelled

```python
from stardag import ExecutionCancelled
```

The other exception you **raise** rather than catch. It says: _this
execution is no longer wanted; stop without producing anything._

Stardag raises it for you at the two automatic cooperative-cancellation
checkpoints — the start of each attempt, and each dynamic-dependency
yield. You raise it where only you know a stop is safe:

```python
import stardag as sd

for chunk in chunks:
    if sd.cancellation_requested():
        raise sd.ExecutionCancelled()
    process(chunk)
```

The worker treats it as a **clean exit, not a task failure**: no output is
written, no completion is reported, and no end-of-attempt event is
recorded. What it does _not_ do is return normally, and that is
deliberate — a backend call that succeeds with no output is read by a
scheduler as "the worker wrote it, eventual consistency", which would
record a completion for a target that does not exist.

Ask `sd.cancellation_requested()` first rather than raising
speculatively: it is throttled, and it answers `True` only when the
registry positively said this execution has been superseded, its task
cancelled, or its build stopped running. See
[Cancelling work](../concepts/build-execution.md#cancelling-work-the-worker-asks-nothing-reaches-in).

Like `ResumableInterruption`, it is an ordinary `Exception` rather than a
`BaseException`, so it does not slip past your own error handling. A task
that catches it should re-raise.

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
