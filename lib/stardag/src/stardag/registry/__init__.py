"""Task registry module for stardag.

This module provides registry implementations for tracking task execution.
The main classes are:

- RegistryABC: Abstract base class defining the registry interface
- APIRegistry: Registry that communicates with the stardag-api service
- NoOpRegistry: A do-nothing registry (default when unconfigured)
- registry_provider: Resource provider for getting the configured registry
- RegistryGlobalConcurrencyLock: GlobalConcurrencyLock implementation using Registry API
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
from stardag.registry._lock import RegistryGlobalConcurrencyLock

__all__ = [
    "APIRegistry",
    "NoOpRegistry",
    "RegisterdTaskEnvelope",
    "RegistryABC",
    "RegistryGlobalConcurrencyLock",
    "get_git_commit_hash",
    "init_registry",
    "registry_provider",
]
