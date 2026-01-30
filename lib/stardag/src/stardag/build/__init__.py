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
"""

from stardag.build._base import (
    BuildExitStatus,
    BuildSummary,
    DefaultGlobalLockSelector,
    FailMode,
    GlobalConcurrencyLockManager,
    GlobalLockConfig,
    GlobalLockSelector,
    LockAcquisitionResult,
    LockAcquisitionStatus,
    LockHandle,
    RoutedTaskExecutor,
    TaskCount,
    TaskExecutorABC,
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
    # Task executors
    "HybridConcurrentTaskExecutor",
    "RoutedTaskExecutor",
    "TaskExecutorABC",
    # Build functions
    "build",
    "build_aio",
    "build_sequential",
    "build_sequential_aio",
]
