"""Build module for stardag.

This module provides functions and classes for building task DAGs.

Primary build functions:
- build(): Sync concurrent build (the default for production)
- build_aio(): Async concurrent build
- build_sequential(): Sync sequential build (for debugging)
- build_sequential_aio(): Async sequential build (for debugging)

Task runner:
- HybridConcurrentTaskRunner: Routes tasks to async/thread/process pools

Interfaces:
- TaskRunnerABC: Abstract base class for custom task runners
- ExecutionModeSelector: Protocol for custom execution mode selection

Global concurrency locking:
- GlobalConcurrencyLockManager: Protocol for distributed lock implementations
- GlobalLockConfig: Configuration for global locking behavior
"""

from stardag.build._base import (
    BuildExitStatus,
    BuildSummary,
    DefaultExecutionModeSelector,
    DefaultGlobalLockSelector,
    DefaultRunWrapper,
    ExecutionMode,
    ExecutionModeSelector,
    FailMode,
    GlobalConcurrencyLockManager,
    GlobalLockConfig,
    GlobalLockSelector,
    LockAcquisitionResult,
    LockAcquisitionStatus,
    LockHandle,
    RunWrapper,
    TaskCount,
    TaskExecutionState,
    TaskRunnerABC,
)
from stardag.build._concurrent import (
    HybridConcurrentTaskRunner,
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
    "TaskExecutionState",
    # Execution mode
    "DefaultExecutionModeSelector",
    "ExecutionMode",
    "ExecutionModeSelector",
    # Run wrapper
    "DefaultRunWrapper",
    "RunWrapper",
    # Global concurrency lock
    "DefaultGlobalLockSelector",
    "GlobalConcurrencyLockManager",
    "GlobalLockConfig",
    "GlobalLockSelector",
    "LockAcquisitionResult",
    "LockAcquisitionStatus",
    "LockHandle",
    # Task runners
    "HybridConcurrentTaskRunner",
    "TaskRunnerABC",
    # Build functions
    "build",
    "build_aio",
    "build_sequential",
    "build_sequential_aio",
]
