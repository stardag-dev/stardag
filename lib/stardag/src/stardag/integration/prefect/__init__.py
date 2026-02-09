"""Prefect integration for Stardag.

This module provides functions to build stardag tasks using Prefect for
orchestration. Prefect handles scheduling, dependency management, observability,
and error handling via its task submission system.
"""

from stardag.integration.prefect._build import (
    AsyncRunCallback,
    build,
    build_aio,
    build_flow,
    create_markdown,
    upload_task_on_complete_artifacts,
)
from stardag.integration.prefect._utils import format_key

__all__ = [
    "AsyncRunCallback",
    "build",
    "build_aio",
    "build_flow",
    "create_markdown",
    "format_key",
    "upload_task_on_complete_artifacts",
]
