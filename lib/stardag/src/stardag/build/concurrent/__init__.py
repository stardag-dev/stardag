"""Concurrent build implementations without Prefect dependency.

This module provides several approaches to concurrent DAG execution:

1. ThreadPoolBuild - Uses ThreadPoolExecutor for concurrent I/O-bound tasks
2. AsyncIOBuild - Uses asyncio for async task execution
3. MultiprocessingBuild - Uses ProcessPoolExecutor for CPU-bound tasks (no dynamic deps)

All implementations support:
- Static dependencies via task.requires()
- Dynamic dependencies via generator-based run() methods (except multiprocessing)
- Registry integration for tracking
- Callbacks for custom handling

Recommended usage:
- For I/O-bound tasks: use build_threadpool (supports dynamic deps)
- For async tasks: use build_async (supports dynamic deps)
- For CPU-bound tasks without dynamic deps: use build_multiprocess
"""

from stardag.build.concurrent.threadpool import build_simple as build_threadpool
from stardag.build.concurrent.asyncio_builder import build as build_async
from stardag.build.concurrent.asyncio_builder import (
    build_queue_based as build_async_queue,
)
from stardag.build.concurrent.multiprocess import build as build_multiprocess

__all__ = [
    "build_threadpool",
    "build_async",
    "build_async_queue",
    "build_multiprocess",
]
