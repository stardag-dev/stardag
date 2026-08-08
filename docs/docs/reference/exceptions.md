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
