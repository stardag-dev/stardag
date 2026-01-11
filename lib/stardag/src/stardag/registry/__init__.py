"""Task registry module for stardag.

This module provides registry implementations for tracking task execution.
The main classes are:

- RegistryABC: Abstract base class defining the registry interface
- APIRegistry: Registry that communicates with the stardag-api service
- NoOpRegistry: A do-nothing registry (default when unconfigured)
- registry_provider: Resource provider for getting the configured registry
- RegistryGlobalConcurrencyLockManager: GlobalConcurrencyLockManager using Registry API
- RegistryLockManagerConfig: Configuration for RegistryGlobalConcurrencyLockManager
- RegistryAPIClientConfig: HTTP client configuration for Registry API
"""

from stardag.registry._api_registry import APIRegistry
from stardag.registry._base import (
    NoOpRegistry,
    RegisterdTaskEnvelope,
    RegistryABC,
    get_git_commit_hash,
    init_registry,
    registry_provider,
)
from stardag.registry._http_client import (
    RegistryAPIAsyncHTTPClient,
    RegistryAPIClientConfig,
    RegistryAPISyncHTTPClient,
    get_async_http_client,
    get_sync_http_client,
    handle_response_error,
)
from stardag.registry._lock import (
    RegistryGlobalConcurrencyLockManager,
    RegistryLockManagerConfig,
)

__all__ = [
    "APIRegistry",
    "NoOpRegistry",
    "RegisterdTaskEnvelope",
    "RegistryABC",
    "RegistryAPIAsyncHTTPClient",
    "RegistryAPIClientConfig",
    "RegistryAPISyncHTTPClient",
    "RegistryGlobalConcurrencyLockManager",
    "RegistryLockManagerConfig",
    "get_async_http_client",
    "get_git_commit_hash",
    "get_sync_http_client",
    "handle_response_error",
    "init_registry",
    "registry_provider",
]
