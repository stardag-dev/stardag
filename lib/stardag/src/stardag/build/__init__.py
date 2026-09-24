"""Build module for stardag.

Primary build functions:
- build(): Concurrent build, recommended for real workloads from a sync context
- build_aio(): Async concurrent build, for an async context or a running loop
- build_sequential(): Sync sequential build (for debugging)
- build_sequential_aio(): Async sequential build (for debugging)

With a registry, every build plans under a scope ``(deployment, settings)``
and every execution claims (see ``docs/design/registry-v2/design.md``);
without one, it runs purely locally.

Task executor:
- HybridConcurrentTaskExecutor: Routes tasks to async/thread/process pools
- RoutedTaskExecutor: Routes tasks between executors

Interfaces:
- TaskExecutorABC: Abstract base class for custom task executors
- ExecutionModeSelector: Protocol for custom execution mode selection

Concurrency limiting:
- ConcurrencyConfig: Build-local overall and named limits
- ConcurrencyLimiter: Protocol for custom limiters

Reactive scheduling (ticks) and task-module declaration:
- run_tick_aio / TickConfig / TickSummary
- expand_task_module_patterns() / import_task_modules() / plan_rehydration()
"""

from stardag.build._base import (
    BuildContext,
    BuildExitStatus,
    BuildFailed,
    BuildStopped,
    BuildSummary,
    ClaimConfig,
    DetachedHandle,
    ExecutorDetails,
    FailMode,
    OnRegistryFailure,
    RoutedTaskExecutor,
    TaskCount,
    TaskExecutionError,
    TaskExecutorABC,
    get_current_build_context,
    get_current_build_id,
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
)
from stardag.build._deployment import DeploymentResolutionError
from stardag.build._reactive import (
    RollOver,
    RollOverFailed,
    TickConfig,
    TickSummary,
    run_tick_aio,
)
from stardag.build._registration import RequiresError
from stardag.build._resident import build_aio
from stardag.build._sequential import build_sequential, build_sequential_aio
from stardag.build._settings import SettingsError
from stardag.build._task_modules import (
    RehydrationPlan,
    TaskModuleImportReport,
    TaskModulesError,
    declared_task_module_patterns,
    expand_task_module_patterns,
    import_task_modules,
    last_import_failures,
    module_is_covered,
    module_is_main,
    plan_rehydration,
    set_declared_task_module_patterns,
    uncovered_task_classes,
    validate_task_module_patterns,
)

__all__ = [
    # Data structures
    "BuildContext",
    "BuildExitStatus",
    "BuildFailed",
    "BuildStopped",
    "BuildSummary",
    "FailMode",
    "OnRegistryFailure",
    "TaskCount",
    # Execution mode
    "DefaultExecutionModeSelector",
    "ExecutionMode",
    "ExecutionModeSelector",
    # Concurrency limiting
    "ConcurrencyConfig",
    "ConcurrencyKeySelector",
    "ConcurrencyLimiter",
    "LocalConcurrencyLimiter",
    "NoOpConcurrencyLimiter",
    # Task executors and claims
    "ClaimConfig",
    "DetachedHandle",
    "ExecutorDetails",
    "HybridConcurrentTaskExecutor",
    "RoutedTaskExecutor",
    "TaskExecutionError",
    "TaskExecutorABC",
    "get_current_build_context",
    "get_current_build_id",
    # Errors
    "DeploymentResolutionError",
    "RequiresError",
    "SettingsError",
    # Reactive scheduling
    "RollOver",
    "RollOverFailed",
    "TickConfig",
    "TickSummary",
    "run_tick_aio",
    # Task-module declaration (reactive scheduling)
    "RehydrationPlan",
    "TaskModuleImportReport",
    "TaskModulesError",
    "declared_task_module_patterns",
    "expand_task_module_patterns",
    "import_task_modules",
    "last_import_failures",
    "module_is_covered",
    "module_is_main",
    "plan_rehydration",
    "set_declared_task_module_patterns",
    "uncovered_task_classes",
    "validate_task_module_patterns",
    # Build functions
    "build",
    "build_aio",
    "build_sequential",
    "build_sequential_aio",
]
