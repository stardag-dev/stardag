"""Build module for stardag.

This module provides functions and classes for building task DAGs.

Primary build functions:
- build(): Concurrent build, recommended for real workloads from a sync context
- build_aio(): Async concurrent build, recommended for real workloads from an
    async context or already running event loop
- build_sequential(): Sync sequential build (for debugging)
- build_sequential_aio(): Async sequential build (for debugging)

Task runner:
- HybridConcurrentTaskRunner: Routes tasks to async/thread/process pools

Interfaces:
- TaskRunnerABC: Abstract base class for custom task runners
- ExecutionModeSelector: Protocol for custom execution mode selection
"""

from stardag.build._base import (
    BuildExitStatus,
    BuildSummary,
    DefaultExecutionModeSelector,
    DefaultRunWrapper,
    ExecutionMode,
    ExecutionModeSelector,
    FailMode,
    RunWrapper,
    TaskCount,
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
    # Execution mode
    "DefaultExecutionModeSelector",
    "ExecutionMode",
    "ExecutionModeSelector",
    # Run wrapper
    "DefaultRunWrapper",
    "RunWrapper",
    # Task runners
    "HybridConcurrentTaskRunner",
    "TaskRunnerABC",
    # Build functions
    "build",
    "build_aio",
    "build_sequential",
    "build_sequential_aio",
]
