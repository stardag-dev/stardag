"""Build module for stardag.

This module provides functions and classes for building task DAGs.

Primary build functions:
- build(): Concurrent build, recommended for real workloads from a sync context
- build_aio(): Async concurrent build, recommended for real workloads from an
    async context or already running event loop
- build_sequential(): Sync sequential build (for debugging)
- build_sequential_aio(): Async sequential build (for debugging)

Task executor:
- HybridConcurrentTaskExecutor: Routes tasks to async/thread/process pools

Interfaces:
- TaskExecutorABC: Abstract base class for custom task executors
- ExecutionModeSelector: Protocol for custom execution mode selection

Global concurrency locking:
- GlobalConcurrencyLockManager: Protocol for distributed lock implementations
- LockHandle: Protocol for lock handles (async context manager)
- GlobalLockConfig: Configuration for global locking behavior

Granular concurrency limiting:
- ConcurrencyConfig: Overall and named limits on concurrently executing tasks
- ConcurrencyLimiter: Protocol for custom/distributed limiters
"""

from stardag.build._base import (
    BuildExitStatus,
    BuildFailed,
    BuildSummary,
    DefaultGlobalLockSelector,
    FailMode,
    GlobalConcurrencyLockManager,
    GlobalLockConfig,
    GlobalLockSelector,
    LockAcquisitionResult,
    LockAcquisitionStatus,
    LockHandle,
    OnRegistryFailure,
    RoutedTaskExecutor,
    TaskCount,
    TaskExecutionError,
    DetachedHandle,
    get_current_build_id,
    TaskExecutorABC,
)
from stardag.build._concurrency import (
    ConcurrencyConfig,
    ConcurrencyKeySelector,
    ConcurrencyLimiter,
    LocalConcurrencyLimiter,
    NoOpConcurrencyLimiter,
)
from stardag.build._concurrent import (
    DefaultExecutionModeSelector,
    ExecutionMode,
    ExecutionModeSelector,
    HybridConcurrentTaskExecutor,
    build,
    build_aio,
)
from stardag.build._sequential import (
    build_sequential,
    build_sequential_aio,
)

__all__ = [
    # Data structures
    "BuildExitStatus",
    "BuildFailed",
    "BuildSummary",
    "FailMode",
    "TaskCount",
    # Execution mode
    "DefaultExecutionModeSelector",
    "ExecutionMode",
    "ExecutionModeSelector",
    # Global concurrency lock
    "DefaultGlobalLockSelector",
    "GlobalConcurrencyLockManager",
    "GlobalLockConfig",
    "GlobalLockSelector",
    "LockAcquisitionResult",
    "LockAcquisitionStatus",
    "LockHandle",
    "OnRegistryFailure",
    # Granular concurrency limiting
    "ConcurrencyConfig",
    "ConcurrencyKeySelector",
    "ConcurrencyLimiter",
    "LocalConcurrencyLimiter",
    "NoOpConcurrencyLimiter",
    # Task executors
    "HybridConcurrentTaskExecutor",
    "RoutedTaskExecutor",
    "TaskExecutionError",
    "DetachedHandle",
    "get_current_build_id",
    "TaskExecutorABC",
    # Build functions
    "build",
    "build_aio",
    "build_sequential",
    "build_sequential_aio",
]
