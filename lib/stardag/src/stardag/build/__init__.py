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

Task-module declaration (reactive scheduling only):
- expand_task_module_patterns()/import_task_modules(): make a scheduler
    process able to reconstruct a build's task classes from registry data
- plan_pickle_elision(): decide which tasks still need a build-task-store
    pickle (see stardag.build._task_modules for the full rationale)
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
    ClaimConfig,
    DetachedExecutionStatus,
    DetachedHandle,
    get_current_build_id,
    TaskExecutorABC,
)
from stardag.build._registry_limiter import RegistryConcurrencyLimiter
from stardag.build._reactive import (
    DiscoveryResult,
    InterruptionPolicy,
    TickConfig,
    TickSummary,
    discover_and_register_aio,
    run_tick_aio,
)
from stardag.build._task_store import BuildTaskStore
from stardag.build._task_modules import (
    PickleElisionPlan,
    TaskModuleImportReport,
    TaskModulesError,
    declared_task_module_patterns,
    expand_task_module_patterns,
    import_task_modules,
    last_import_failures,
    module_is_covered,
    plan_pickle_elision,
    set_declared_task_module_patterns,
    uncovered_task_classes,
    validate_task_module_patterns,
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
    "BuildTaskStore",
    # Task-module declaration (reactive scheduling)
    "PickleElisionPlan",
    "TaskModuleImportReport",
    "TaskModulesError",
    "declared_task_module_patterns",
    "expand_task_module_patterns",
    "import_task_modules",
    "last_import_failures",
    "module_is_covered",
    "plan_pickle_elision",
    "set_declared_task_module_patterns",
    "uncovered_task_classes",
    "validate_task_module_patterns",
    "ClaimConfig",
    "DetachedExecutionStatus",
    "DiscoveryResult",
    "InterruptionPolicy",
    "TickConfig",
    "TickSummary",
    "discover_and_register_aio",
    "run_tick_aio",
    "RegistryConcurrencyLimiter",
    "DetachedHandle",
    "get_current_build_id",
    "TaskExecutorABC",
    # Build functions
    "build",
    "build_aio",
    "build_sequential",
    "build_sequential_aio",
]
