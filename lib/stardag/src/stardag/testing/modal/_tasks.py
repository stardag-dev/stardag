"""Simple test tasks for Modal integration tests.

These tasks are defined inside the stardag package (not in tests/) so they
can be deserialized in Modal containers that install stardag from local source.
"""

import stardag as sd


@sd.task
def make_range(limit: int) -> list[int]:
    """Return list(range(limit))."""
    return list(range(limit))


@sd.task
def sum_list(values: sd.Depends[list[int]]) -> int:
    """Return sum of input list."""
    return sum(values)
